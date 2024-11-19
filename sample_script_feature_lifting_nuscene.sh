DATE=`date '+%m%d'`

scene_idx=001
start_timestep=0
end_timestep=75
# reduce num_iters to 8000 for debugging
num_iters=25000

output_root="./work_dirs/$DATE"
project=feature_lifting_nuscenes
export CUDA_VISIBLE_DEVICES=0
# use default_config.yaml for static scenes
# for novel view synthesis, change test_image_stride to 10
python train_emernerf.py \
    --config_file configs/default_dynamic.yaml \
    --output_root $output_root \
    --project $project \
    --run_name ${scene_idx}_flow_nuscene \
    --render_data_video \
    data.data_root="data/nuscenes" \
    data.dataset="nuscenes" \
    data.scene_idx=$scene_idx \
    data.pixel_source.num_cams=6 \
    data.pixel_source.load_size=[450,800] \
    data.pixel_source.skip_feature_extraction=False \
    data.pixel_source.load_features=True \
    data.pixel_source.feature_model_type=dinov2_vitb14 \
    data.lidar_source.truncated_min_range=-60 \
    data.start_timestep=$start_timestep \
    data.end_timestep=$end_timestep \
    nerf.model.head.enable_feature_head=True \
    logging.saveckpt_freq=$num_iters \
    optim.num_iters=$num_iters