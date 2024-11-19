"""
@file   renderer_mixin.py
@author Jianfei Guo, Shanghai AI Lab
@brief  Rendering mixin for NeuS with acceleration.

NOTE: Different NeuS implementations:

In the original NeuS implementation, for each network query point, \
    its alpha-value is calculated based on differences between CDF values of two points \
    extending half an interval forward and extending half an interval backward respectively. \
These two points' SDF values are estimated using the current point's SDF and nablas values.

>>> NeuS original implmentaion

                Interval's alpha, using estimated SDFs of two middle points
                        {  - - - - - - - - - - - - - - - - - }
                        
    | ----------------- o ----------------- | -------------- o -------------- |
    ^                   ^                   ^                ^                ^
Previous point    Point extending    Current point    Point extending     Next point
                  half an interval                    half an interval
                  backward                            forward
                        V                   V                V
                  Estimated SDF      Queried SDF,     Estimated SDF
                                     nablas, colors



However, in practice, nablas of multires-grid-based representations are often much wilder than original MLPs, \
    which brings a lot of noise to the estimation and unstablizes training.

Hence, in our implementation, we first query interval boundaries' SDF to calculate alphas, \
    then query middle points' color and nablas for rendering.

>>> Our implmentation in nr3d_lib / neuralsim / neuralgen / neurecon

               Interval's alpha                       Interval's alpha      
          using "real" queried SDFs              using "real" queried SDFs  
    { - - - - - - - - - - - - - - - - - - -} { - - - - - - - - - - - - - - - -}
    
    | ----------------- x ----------------- | -------------- x -------------- |
    ^                   ^                   ^                ^                ^
Boundary pts       Middle pts          Boundary pts      Middle pts      Boundary pts
    V                   V                   V                V                V
 Queried SDF       Queried             Queried SDF       Queried         Quereid SDF
                   nablas, colors                        nablas colors

"""

__all__ = [
    'NeusRendererMixin'
]

import numpy as np
from operator import itemgetter
from typing import Dict, List, Tuple, Union

import torch
import torch.nn.functional as F
from torch.utils.benchmark import Timer

from nr3d_lib.fmt import log
from nr3d_lib.logger import Logger
from nr3d_lib.profile import profile

from nr3d_lib.models.model_base import ModelMixin
from nr3d_lib.models.utils import batchify_query
from nr3d_lib.models.annealers import get_annealer
from nr3d_lib.models.spatial import AABBSpace
from nr3d_lib.models.accelerations import get_accel, accel_types_single_t

from nr3d_lib.graphics.pack_ops import packed_div, packed_sum
from nr3d_lib.graphics.nerf import packed_alpha_to_vw, ray_alpha_to_vw
from nr3d_lib.graphics.neus import *

class NeusRendererMixin(ModelMixin):
    """
    NeuS Renderer Mixin class
    
    NOTE: This is a mixin class!
    Refer: https://stackoverflow.com/questions/533631/what-is-a-mixin-and-why-are-they-useful
    !!!!: The target class for this mixin should also inherit from `ModelMixin`.
    """
    
    # NOTE: Configuration for common information usage in the forward process (and their defaults)
    use_view_dirs: bool = False # Determines if view_dirs are needed in forward process
    use_nablas: bool = False # Determines if nablas are required in forward process
    use_h_appear: bool = False # Determines if global per-frame appearance embeddings are necessary in forward process
    use_ts: bool = False # Determines if global timestamps are used in forward process
    use_pix: bool = False # Determines if pixel locations (in range [0,1]^2) are required in forward process
    use_fidx: bool = False # Determines if global frame indices are used in forward process
    use_bidx: bool = False # Determines if batch indices are used in forward process
    fwd_sdf_use_pix: bool = False
    fwd_sdf_use_h_appear: bool = False
    fwd_sdf_use_view_dirs: bool = False
    
    def __init__(
        self, *, 
        # Renderer mixin kwargs
        ray_query_cfg: dict = dict(), 
        accel_cfg: dict = None, 
        sample_anneal_cfg: dict = None, 
        shrink_milestones: List[int]=[], 
        # Network kwargs
        **net_kwargs) -> None:
        
        mro = type(self).mro()
        super_class = mro[mro.index(NeusRendererMixin)+1]
        assert super_class is not ModelMixin, "Incorrect class inheritance. Three possible misuse scenarios:\n"\
            "Case 1: The Net class for mixin should also inherit from `ModelMixin`.\n"\
            "Case 2: RendererMixin should come before the Net class when inheriting.\n"\
            "Case 3: You should not directly instantiate this mixin class."
        
        super().__init__(**net_kwargs) # Will call network's __init__() (e.g. LoTDNeus ...)
        
        self.ray_query_cfg = ray_query_cfg
        self.accel_cfg = accel_cfg
        self.sample_anneal_cfg = sample_anneal_cfg
        
        # Shrink
        self.shrink_milestones = shrink_milestones

    def populate(self, *args, **kwargs):
        super().populate(*args, **kwargs)
        
        # Acceleration struct
        self.accel: accel_types_single_t = None if self.accel_cfg is None \
            else get_accel(space=self.space, device=self.device, **self.accel_cfg)
        self.upsample_s_divisor = 1.0
        
        # Sample annealing
        self.sample_ctrl = get_annealer(**self.sample_anneal_cfg) if self.sample_anneal_cfg is not None else None

    # @property
    # def space(self) -> AABBSpace:
    #     return super().space

    def sample_pts_uniform(self, num_samples: int):
        # NOTE: Returns normalized `x`
        x = self.space.sample_pts_uniform(num_samples)
        ret = self.forward_sdf_nablas(x, skip_accel=True) # Do not upsate_samples here (usally there are too less samples here.)
        ret = {k: v.to(x.dtype) for k, v in ret.items()}
        ret['net_x'] = x # NOTE: in network's uniformed space; not in world space.
        return ret

    def sample_pts_in_occupied(self, num_samples: int):
        assert self.accel is not None, "Requires self.accel not to be None"
        x = self.accel.sample_pts_in_occupied(num_samples) # [-1,1]
        ret = self.forward_sdf_nablas(x, skip_accel=True) # Do not upsate_samples here (usally there are too less samples here.)
        ret = {k: v.to(x.dtype) for k, v in ret.items()}
        ret['net_x'] = x # NOTE: in network's uniformed space; not in world space.
        return ret

    def forward_sdf(self, x: torch.Tensor, skip_accel=False, **kwargs):
        ret = super().forward_sdf(x, **kwargs)
        if self.training and (not skip_accel) and (self.accel is not None):
            self.accel.collect_samples(x, val=ret['sdf'].data)
        return ret
    
    def forward_sdf_nablas(self, x: torch.Tensor, skip_accel=False, **kwargs):
        ret = super().forward_sdf_nablas(x, **kwargs)
        if self.training and (not skip_accel) and (self.accel is not None):
            self.accel.collect_samples(x, val=ret['sdf'].data)
        return ret

    @torch.no_grad()
    def query_sdf(self, x: torch.Tensor, **kwargs):
        return super().query_sdf(x, **kwargs)

    def training_initialize(self, config=dict(), logger=None, log_prefix: str=None, skip_accel=False) -> bool:
        updated = super().training_initialize(config, logger=logger, log_prefix=log_prefix)
        if (not skip_accel) and (self.accel is not None):
            self.accel.init(self.query_sdf, logger=logger)
        return updated

    def training_before_per_step(self, cur_it: int, logger: Logger = None, skip_accel=False):
        self.it = cur_it
        super().training_before_per_step(cur_it, logger=logger)
        if (not skip_accel) and (self.accel is not None):
            self.upsample_s_divisor = 2 ** self.accel.training_granularity
            if self.training:
                self.accel.step(cur_it, self.query_sdf, logger)
                # if cur_it == 0:
                #     self.accel.init(self.query_sdf, logger)
                # else:
                #     self.accel.step(cur_it, self.query_sdf, logger)

    def training_after_per_step(self, cur_it: int, logger: Logger = None, skip_accel=False):
        super().training_after_per_step(cur_it, logger=logger)
        if (not skip_accel) and (self.accel is not None):
            #------------ Shrink according to actual occupied space.
            if cur_it in self.shrink_milestones:
                self.shrink()

    @torch.no_grad()
    def shrink(self):
        old_aabb = self.space.aabb
        new_aabb = self.accel.try_shrink()
        # Rescale network
        super().rescale_volume(new_aabb)
        # Rescale acceleration struct
        self.accel.rescale_volume(new_aabb)
        # Rescale space
        # NOTE: Always rescale space at the last step, since the old space is required by prev steps
        self.space.rescale_volume(new_aabb)
        old_aabb_str = '[' + ', '.join([str(np.round(i, decimals=2)) for i in old_aabb.tolist()]) + ']'
        new_aabb_str = '[' + ', '.join([str(np.round(i, decimals=2)) for i in new_aabb.tolist()]) + ']'
        log.info(f"=> Shrink from {old_aabb_str} to {new_aabb_str}")

    def ray_test(self, rays_o: torch.Tensor, rays_d: torch.Tensor, near=None, far=None, **extra_ray_data: Dict[str, torch.Tensor]):
        """ Test input rays' intersection with the model's space (AABB, Blocks, etc.)

        Args:
            rays_o (torch.Tensor): [num_total_rays,3] Input rays' origin
            rays_d (torch.Tensor): [num_total_rays,3] Input rays' direction
            near (Union[float, torch.Tensor], optional): [num_total_rays] tensor or float. Defaults to None.
            far (Union[float, torch.Tensor], optional): [num_total_rays] tensor or float. Defaults to None.
        Returns:
            dict: The ray_tested result. An example dict:
                num_rays:   int, Number of tested rays
                rays_inds:  [num_rays] tensor, ray indices in `num_total_rays` of the tested rays
                rays_o:     [num_rays, 3] tensor, the indexed and scaled rays' origin
                rays_d:     [num_rays, 3] tensor, the indexed and scaled rays' direction
                near:       [num_rays] tensor, entry depth of intersection
                far:        [num_rays] tensor, exit depth of intersection
        """
        assert (rays_o.dim() == 2) and (rays_d.dim() == 2), "Expect `rays_o` and `rays_d` to be of shape [N, 3]"
        for k, v in extra_ray_data.items():
            if isinstance(v, torch.Tensor):
                assert v.shape[0] == rays_o.shape[0], f"Expect `{k}` has the same shape prefix with rays_o {rays_o.shape[0]}, but got {v.shape[0]}"
        return self.space.ray_test(rays_o, rays_d, near=near, far=far, return_rays=True, **extra_ray_data)

    @profile
    def ray_query(
        self, 
        # ray query inputs
        ray_input: Dict[str, torch.Tensor]=None, ray_tested: Dict[str, torch.Tensor]=None, 
        # ray query function config
        config=dict(), 
        # function config
        return_buffer=False, return_details=False, render_per_obj_individual=False) -> dict:
        """ Query the model with input rays. 
            Conduct the core ray sampling, ray marching, ray upsampling and network query operations.

        Args:
            ray_input (Dict[str, torch.Tensor], optional): All input rays.
                See more details in `ray_test`
            ray_tested (Dict[str, torch.Tensor], optional): Tested rays (Typicallly those that intersect with objects). A dict composed of:
                num_rays:   int, Number of tested rays
                rays_inds:   [num_rays] tensor, ray indices in `num_total_rays` of the tested rays
                rays_o:     [num_rays, 3] tensor, the indexed and scaled rays' origin
                rays_d:     [num_rays, 3] tensor, the indexed and scaled rays' direction
                near:       [num_rays] tensor, entry depth of intersection
                far:        [num_rays] tensor, exit depth of intersection
            config (dict, optional): Config of ray_query. Defaults to dict().
            return_buffer (bool, optional): If return the queried volume buffer. Defaults to False.
            return_details (bool, optional): If return training / debugging related details. Defaults to False.
            render_per_obj_individual (bool, optional): If return single object / seperate volume rendering results. Defaults to False.

        Returns:
            nested dict: The queried results, including 'volume_buffer', 'details', 'rendered'.
            
            ['volume_buffer']: dict, The queried volume buffer. Available if `return_buffer` is set True.
                For now, two types of buffers might be queried depending on the ray sampling algorithms, 
                    namely `batched` buffers and `packed` buffers.
                
                If there are no tested rays or no hit rays, the returned buffer is empty:
                    'volume_buffer" {'type': 'empty'}
                
                An example `batched` buffer:
                    'volume_buffer': {
                        'type': 'batched',
                        'rays_inds_hit':     [num_rays_hit] tensor, ray indices in `num_total_rays` of the hit & queried rays
                        't':                [num_rays_hit, num_samples_per_ray] batched tensor, real depth of the queried samples
                        'opacity_alpha':    [num_rays_hit, num_samples_per_ray] batched tensor, the queried alpha-values
                        'rgb':              [num_rays_hit, num_samples_per_ray, 3] packed tensor, optional, the queried rgb values (Only if `with_rgb` is True)
                        'nablas':           [num_rays_hit, num_samples_per_ray, 3] packed tensor, optional, the queried nablas values (Only if `with_normal` is True)
                        'feature':          [num_rays_hit, num_samples_per_ray, with_feature_dim] batched tensor, optional, the queried features (Only if `with_feature_dim` > 0)
                    }
                
                An example `packed` buffer:
                    'volume_buffer': {
                        'type': 'packed',
                        'rays_inds_hit':     [num_rays_hit] tensor, ray indices in `num_total_rays` of the hit & queried rays
                        'pack_infos_hit'    [num_rays_hit, 2] tensor, pack infos of the queried packed tensors
                        't':                [num_packed_samples] packed tensor, real depth of the queried samples
                        'opacity_alpha':    [num_packed_samples] packed tensor, the queried alpha-values
                        'rgb':              [num_packed_samples, 3] packed tensor, optional, the queried rgb values (Only if `with_rgb` is True)
                        'nablas':           [num_packed_samples, 3] packed tensor, optional, the queried nablas values (Only if `with_normal` is True)
                        'feature':          [num_packed_samples, with_feature_dim] packed tensor, optional, the queried features (Only if `with_feature_dim` > 0)
                    }

            ['details']: nested dict, Details for training. Available if `return_details` is set True.
            
            ['rendered']: dict, stand-alone rendered results. Available if `render_per_obj_individual` is set True.
                An example rendered dict:
                    'rendered' {
                        'mask_volume':      [num_total_rays,] The rendered opacity / occupancy, in range [0,1]
                        'depth_volume':     [num_total_rays,] The rendered real depth
                        'rgb_volume':       [num_total_rays, 3] The rendered rgb, in range [0,1] (Only if `with_rgb` is True)
                        'normals_volume':   [num_total_rays, 3] The rendered normals, in range [-1,1] (Only if `with_normal` is True)
                        'feature_volume':   [num_total_rays, with_feature_dim] The rendered feature. (Only if `with_feature_dim` > 0)
                    }
        """
        
        #----------------
        # Inputs
        #----------------
        if ray_tested is None:
            assert ray_input is not None
            ray_tested = self.ray_test(**ray_input)
        
        #----------------
        # Shortcuts
        #----------------
        # NOTE: The device & dtype of output
        device, dtype = self.device, torch.float
        query_mode, with_rgb, with_normal = config.query_mode, config.with_rgb, config.with_normal
        forward_inv_s = config.get('forward_inv_s', self.forward_inv_s())
        # forward_inv_s = upsample_inv_s / 4.
        
        #----------------
        # Prepare outputs & compute outputs that are needed even when (num_rays==0)
        #----------------
        raw_ret = dict()
        if return_buffer:
            raw_ret['volume_buffer'] = dict(type='empty', rays_inds_hit=[])
        if return_details:
            details = raw_ret['details'] = {}
            if (self.accel is not None) and hasattr(self.accel, 'debug_stats'):
                details['accel'] = self.accel.debug_stats()
            details['inv_s'] = forward_inv_s.item() if isinstance(forward_inv_s, torch.Tensor) else forward_inv_s
            details['s'] = 1./ details['inv_s']
            if hasattr(self, 'radiance_net') and hasattr(self.radiance_net, 'blocks') \
                and hasattr(self.radiance_net.blocks, 'lipshitz_bound_full'):
                details['radiance.lipshitz_bound'] = self.radiance_net.blocks.lipshitz_bound_full().item()
                
        if render_per_obj_individual:
            prefix_rays = ray_input['rays_o'].shape[:-1]
            raw_ret['rendered'] = rendered = dict(
                depth_volume = torch.zeros([*prefix_rays], dtype=dtype, device=device),
                mask_volume = torch.zeros([*prefix_rays], dtype=dtype, device=device),
            )
            if with_rgb:
                rendered['rgb_volume'] = torch.zeros([*prefix_rays, 3], dtype=dtype, device=device)
            if with_normal:
                rendered['normals_volume'] = torch.zeros([*prefix_rays, 3], dtype=dtype, device=device)

        if ray_tested['num_rays'] == 0:
            return raw_ret
        
        #----------------
        # Ray query
        #----------------
        if query_mode == 'march_occ_multi_upsample':
            volume_buffer, query_details = neus_ray_query_march_occ_multi_upsample(
                self, ray_tested, with_rgb=with_rgb, with_normal=with_normal, 
                upsample_s_divisor=self.upsample_s_divisor, 
                perturb=config.perturb, forward_inv_s=forward_inv_s, **config.query_param)
        elif query_mode == 'march_occ_multi_upsample_compressed':
            volume_buffer, query_details = neus_ray_query_march_occ_multi_upsample_compressed(
                self, ray_tested, with_rgb=with_rgb, with_normal=with_normal, 
                upsample_s_divisor=self.upsample_s_divisor, 
                perturb=config.perturb, forward_inv_s=forward_inv_s, **config.query_param)
        elif query_mode == 'march_occ_multi_upsample_compressed_strategy':
            raise NotImplementedError
        elif query_mode == 'coarse_multi_upsample':
            volume_buffer, query_details = neus_ray_query_coarse_multi_upsample(
                self, ray_tested, with_rgb=with_rgb, with_normal=with_normal, 
                upsample_s_divisor=self.upsample_s_divisor, 
                perturb=config.perturb, forward_inv_s=forward_inv_s, **config.query_param)
        elif query_mode == 'march_occ':
            raise NotImplementedError
        elif query_mode == "sphere_trace":
            volume_buffer, query_details = neus_ray_query_sphere_trace(
                self, ray_tested, with_rgb=with_rgb, with_normal=with_normal,
                upsample_s_divisor=self.upsample_s_divisor, 
                perturb=config.perturb, **config.query_param)
        else:
            raise RuntimeError(f"Invalid query_mode={query_mode}")

        #----------------
        # Calc outputs
        #----------------
        if return_buffer:
            raw_ret['volume_buffer'] = volume_buffer
        
        if return_details:
            details.update(query_details)
            if config.get('with_near_sdf', False):
                fwd_kwargs = dict(x=torch.addcmul(ray_tested['rays_o'], ray_tested['rays_d'], ray_tested['near'].unsqueeze(-1)))
                if self.use_ts: fwd_kwargs['ts'] = ray_tested['rays_ts']
                if self.use_fidx: fwd_kwargs['fidx'] = ray_tested['rays_fidx']
                details['near_sdf'] = self.forward_sdf(**fwd_kwargs)['sdf']
        
        if render_per_obj_individual:
            with profile("Render per-object"):
                if (buffer_type:=volume_buffer['type']) != 'empty':
                    rays_inds_hit = volume_buffer['rays_inds_hit']
                    depth_use_normalized_vw = config.get('depth_use_normalized_vw', True)
                    
                    if buffer_type == 'batched':
                        volume_buffer['vw'] = vw = ray_alpha_to_vw(volume_buffer['opacity_alpha'])
                        rendered['mask_volume'][rays_inds_hit] = vw_sum = vw.sum(dim=-1)
                        if depth_use_normalized_vw:
                            # TODO: This can also be differed by training / non-training
                            vw_normalized = vw / (vw_sum.unsqueeze(-1)+1e-10)
                            rendered['depth_volume'][rays_inds_hit] = (vw_normalized * volume_buffer['t']).sum(dim=-1)
                        else:
                            rendered['depth_volume'][rays_inds_hit] = (vw * volume_buffer['t']).sum(dim=-1)
                            
                        if with_rgb:
                            rendered['rgb_volume'][rays_inds_hit] = (vw.unsqueeze(-1) * volume_buffer['rgb']).sum(dim=-2)
                        if with_normal:
                            # rendered['normals_volume'][rays_inds_hit] = (vw.unsqueeze(-1) * volume_buffer['nablas']).sum(dim=-2)
                            if self.training:
                                rendered['normals_volume'][rays_inds_hit] = (vw.unsqueeze(-1) * volume_buffer['nablas']).sum(dim=-2)
                            else:
                                rendered['normals_volume'][rays_inds_hit] = (vw.unsqueeze(-1) * F.normalize(volume_buffer['nablas'].clamp_(-1,1), dim=-1)).sum(dim=-2)
                    elif buffer_type == 'packed':
                        pack_infos_hit = volume_buffer['pack_infos_hit']
                        # [num_sampels]
                        volume_buffer['vw'] = vw = packed_alpha_to_vw(volume_buffer['opacity_alpha'], pack_infos_hit)
                        # [num_rays_hit]
                        rendered['mask_volume'][rays_inds_hit] = vw_sum = packed_sum(vw.view(-1), pack_infos_hit)
                        # [num_samples]
                        if depth_use_normalized_vw:
                            vw_normalized = packed_div(vw, vw_sum + 1e-10, pack_infos_hit)
                            rendered['depth_volume'][rays_inds_hit] = packed_sum(vw_normalized * volume_buffer['t'].view(-1), pack_infos_hit)
                        else:
                            rendered['depth_volume'][rays_inds_hit] = packed_sum(vw.view(-1) * volume_buffer['t'].view(-1), pack_infos_hit)
                        if with_rgb:
                            rendered['rgb_volume'][rays_inds_hit] = packed_sum(vw.view(-1,1) * volume_buffer['rgb'].view(-1,3), pack_infos_hit)
                        if with_normal:
                            # rendered['normals_volume'][rays_inds_hit] = packed_sum(vw.view(-1,1) * volume_buffer['nablas'].view(-1,3), pack_infos_hit)
                            if self.training:
                                rendered['normals_volume'][rays_inds_hit] = packed_sum(vw.view(-1,1) * volume_buffer['nablas'].view(-1,3), pack_infos_hit)
                            else:
                                rendered['normals_volume'][rays_inds_hit] = packed_sum(vw.view(-1,1) * F.normalize(volume_buffer['nablas'].clamp_(-1,1), dim=-1).view(-1,3), pack_infos_hit)
        return raw_ret
