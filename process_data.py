import os
import shutil

def process(input_dir, output_dir):
    # List of steps as integers
    list_step = [16, 21, 22, 25, 31, 34, 35, 49, 53, 80, 84, 86, 89, 94, 96, 102, 111, 222, 323, 382, 402, 427, 438, 546, 581, 592, 620, 640, 700, 754, 795, 796]

    # Ensure output directory exists
    os.makedirs(output_dir, exist_ok=True)

    for filename in os.listdir(input_dir):
        # Check if the filename is a number with 3 digits
        if filename.isdigit() and len(filename) == 3:
            file_num = int(filename)
            if file_num not in list_step:
                # Build full file path
                file_path = os.path.join(input_dir, filename)
                # Move file to output directory
                shutil.move(file_path, output_dir)
                print(f"Moved {filename} to {output_dir}")

# Example usage
input_directory = "/home/ubuntu/Workspace/phat-intern-dev/VinAI/EmerNeRF/data/waymo/processed/training"
output_directory = "/home/ubuntu/Workspace/phat-intern-dev/VinAI/EmerNeRF/data/waymo/processed/remain"
process(input_directory, output_directory)