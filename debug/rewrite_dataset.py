import tensorflow as tf 
import tensorflow_datasets as tfds
import numpy as np
import matplotlib.pyplot as plt
import os
import glob 
import pickle 
import argparse
import sys
import dlimp as dl
from functools import partial
from typing import Callable, Mapping, Optional, Sequence, Tuple, Union

# import octo.data.obs_transforms as obs_transforms
# from octo.data.dataset import apply_frame_transforms

tf.config.run_functions_eagerly(True)
tf.data.experimental.enable_debug_mode()
print(tf.executing_eagerly())


DATASETS = [
    "cory_hall",
    "go_stanford_cropped",
    "go_stanford2",
    "recon",
    "sacson",
    "scand",
    "seattle",
    "tartan_drive",
]

def lookup_in_dict(key_tensor, dictionary):
  """
  Looks up a string key tensor in a Python dictionary.

  Args:
    key_tensor: A tf.string tensor representing the key to lookup.
    dictionary: A Python dictionary with string keys.

  Returns:
    A tf.string tensor representing the value associated with the key,
    or an empty string tensor if the key is not found.
  """
  def lookup(key):
    return dictionary.get(key.decode(), "")

  return tf.py_function(
      func=lookup, 
      inp=[key_tensor], 
      Tout=tf.string
  )

# Fix issues with dataset from TFrecords 
def fix_dataset(traj, traj_info):
    
    # Get the metadata for this traj 
    traj_name = tf.strings.split(traj["traj_metadata"]["episode_metadata"]["file_path"], "/")[-1]
    tf.print(traj_name, output_stream=sys.stdout)
    traj_base_name = tf.strings.split(traj_name, "_start_")[0]
    tf.print(traj_base_name, output_stream=sys.stdout)
    traj_start = tf.cast(tf.strings.split(tf.strings.split(traj_name, "_start_")[-1], "_end_")[0], tf.int32)[0]
    tf.print(traj_start, output_stream=sys.stdout)
    traj_end = tf.cast(tf.strings.split(tf.strings.split(traj_name, "_end_")[-1], "_")[0], tf.int32)[0]
    tf.print(traj_end, output_stream=sys.stdout)

    # Modify the traj info for this trajectory
    curr_traj_info = lookup_in_dict(traj_base_name, traj_info)
    tf.print(curr_traj_info, output_stream=sys.stdout)

    # Check the number of non-white images in the traj
    images = traj["observation_decoded"]["image_decoded"]
    image_non_white = tf.reduce_any(tf.not_equal(images, 255), axis=-1)
    num_non_white = tf.cast(tf.reduce_sum(tf.cast(image_non_white, tf.float32)), tf.int32)

    # Check two things: 
    # 1. Is the spacing between points close to that of the expected normalization factor
    # 2. Modify the yaw such that is closer to the original traj yaw

    # Check the spacing between points
    traj_pos = traj["observation"]["position"]
    traj_pos = tf.cast(traj_pos, tf.float32)
    deltas = tf.linalg.norm(traj_pos[:-1] - traj_pos[1:], axis=-1)
    spacing = tf.reduce_mean(deltas)
    normalization_factor = tf.cast(lookup_in_dict("normalization_factor", curr_traj_info), tf.float32)
    tf.print(f"Spacing for {traj_base_name} is {spacing} and normalization factor is {normalization_factor}")
    if tf.abs(spacing - normalization_factor) > 0.05:
        tf.print(f"Spacing issue for {traj_base_name} with spacing {spacing} and normalization factor {normalization_factor}")
    
    # Check the yaw
    traj_yaw = traj["observation"]["yaw"]
    non_cf_yaw = traj_yaw[:, :num_non_white]
    orig_yaw = tf.cast(lookup_in_dict("yaw", curr_traj_info), tf.float32)
    end = tf.minimum(traj_start + num_non_white, traj_end)
    curr_orig_yaw = orig_yaw[:, traj_start:end+1]

    tf.debugging.assert_equal(tf.shape(non_cf_yaw, 0), tf.shape(curr_orig_yaw, 0), message=f"Length mismatch for {traj_base_name}")

    # Compute the yaw of the original part of the trajectory 
    new_yaw = orig_yaw[traj_start:end + 1]

    # If the trajectory has a counterfactual, we need to generate the correct yaw for the counterfactual part
    if tf.strings.regex_full_match(traj_name, ".*cf.*"):
        cf_start = end - num_non_white
        cf_end = traj_end
        cf_orig_yaw = orig_yaw[traj_start:cf_start]
        cf_new = tf.atan2(traj_pos[cf_start+1:, 1] - traj_pos[cf_start:-1, 1], traj_pos[cf_start+1:, 0] - traj_pos[cf_start:-1, 0]) + cf_orig_yaw[:, -1]
        new_yaw = tf.concat([new_yaw, cf_new], axis=0)
        tf.print(new_yaw)
    
    traj["observation"]["yaw"] = new_yaw
    traj["observation"]["yaw_rotmat"] = tf.stack([tf.cos(new_yaw), -tf.sin(new_yaw), tf.sin(new_yaw), tf.cos(new_yaw)], axis=-1)
    breakpoint()
    return traj
        
def decode(
    obs: dict,
) -> dict:
    """Decodes images and depth images, and then optionally resizes them."""

    image = obs["image"]
    if image.dtype == tf.string:
        if tf.strings.length(image) == 0:
            # this is a padding image
            image = tf.zeros((128, 128, 3), dtype=tf.uint8)
        else:
            image = tf.io.decode_image(
                image, expand_animations=False, dtype=tf.uint8
            )
    
    obs[f"image_decoded"] = image

    return obs

def apply_obs_transform(fn: Callable[[dict], dict], frame: dict) -> dict:
    frame["observation_decoded"] = fn(frame["observation"])
    return frame

# @tf.py_function(Tout=tfds.features.FeaturesDict({
#                 'steps': tfds.features.Dataset({
#                     'observation': tfds.features.FeaturesDict({
#                         'image': tfds.features.Image(
#                             shape=(128, 128, 3),
#                             dtype=np.uint8,
#                             encoding_format='png',
#                             doc='Main camera RGB observation.',
#                         ),
#                         'state': tfds.features.Tensor(
#                             shape=(3,),
#                             dtype=np.float64,
#                             doc='Robot state, consists of [2x position, 1x yaw]',
#                         ),
#                         'position': tfds.features.Tensor(
#                             shape=(2,),
#                             dtype=np.float64,
#                             doc='Robot position',
#                         ),
#                         'yaw': tfds.features.Tensor(
#                             shape=(1,),
#                             dtype=np.float64,
#                             doc='Robot yaw',
#                         ),
#                         'yaw_rotmat': tfds.features.Tensor(
#                             shape=(3, 3),
#                             dtype=np.float64,
#                             doc='Robot yaw rotation matrix',
#                         ),

#                     }),
#                     'action': tfds.features.Tensor(
#                         shape=(2,),
#                         dtype=np.float64,
#                         doc='Robot action, consists of 2x position'
#                     ),
#                      'action_angle': tfds.features.Tensor(
#                         shape=(3,),
#                         dtype=np.float64,
#                         doc='Robot action, consists of 2x position, 1x yaw',
#                     ),

#                     'discount': tfds.features.Scalar(
#                         dtype=np.float64,
#                         doc='Discount if provided, default to 1.'
#                     ),
#                     'reward': tfds.features.Scalar(
#                         dtype=np.float64,
#                         doc='Reward if provided, 1 on final step for demos.'
#                     ),
#                     'is_first': tfds.features.Scalar(
#                         dtype=np.bool_,
#                         doc='True on first step of the episode.'
#                     ),
#                     'is_last': tfds.features.Scalar(
#                         dtype=np.bool_,
#                         doc='True on last step of the episode.'
#                     ),
#                     'is_terminal': tfds.features.Scalar(
#                         dtype=np.bool_,
#                         doc='True on last step of the episode if it is a terminal step, True for demos.'
#                     ),
#                     'language_instruction': tfds.features.Tensor(
#                         shape=(10,),
#                         dtype=tf.string,
#                         doc='Language Instruction.'
#                     ),
#                 }),
#                 'episode_metadata': tfds.features.FeaturesDict({
#                     'file_path': tfds.features.Text(
#                         doc='Path to the original data file.'
#                     ),
#                     'episode_id': tfds.features.Scalar(
#                         dtype=tf.int32,
#                         doc='Episode ID.'
#                     ),
#                 }),
#             }))
def reorganize_traj(traj):
    new_traj = {}

    # Observation
    images = traj["observation"]["image"]
    states = traj["observation"]["state"]
    position = traj["observation"]["position"]
    yaws = traj["observation"]["yaw"]
    yaw_rotmat = traj["observation"]["yaw_rotmat"]

    # Actions
    actions = traj["action"]
    action_angles = traj["action_angle"]
    discount = traj["discount"]
    reward = traj["reward"]
    is_first = traj["is_first"]
    is_last = traj["is_last"]
    is_terminal = traj["is_terminal"]
    language_instruction = traj["language_instruction"]

    num_steps = tf.shape(images)[0]

    def extract_step(i):
        return {"observation": {"image": tfds.features.Image(images[i,...]),
                            "state" : tfds.features.Tensor(states[i,...]),
                            "position": tfds.features.Tensor(position[i,...]),
                            "yaw": tfds.features.Tensor(yaws[i,]),
                            "yaw_rotmat": tfds.features.Tensor(yaw_rotmat[i,...]),
                            },
            "action": tfds.features.Tensor(actions[i,...]),
            "action_angle": tfds.features.Tensor(action_angles[i,...]),
            "discount": tfds.features.Scalar(discount[i,...]),
            "reward": tfds.features.Scalar(reward[i,...]),
            "is_first": tfds.features.Scalar(is_first[i,...]),
            "is_last": tfds.features.Scalar(is_last[i,...]),
            "is_terminal": tfds.features.Scalar(is_terminal[i,...]),
            "language_instruction": tfds.features.Tensor(language_instruction[i,...]),
        }

    # Vectorized map over the first dimension (steps)
    steps = tf.map_fn(
        extract_step, tf.range(num_steps), fn_output_signature={
                    'observation': tfds.features.FeaturesDict({
                        'image': tfds.features.Image(
                            shape=(128, 128, 3),
                            dtype=np.uint8,
                            encoding_format='png',
                            doc='Main camera RGB observation.',
                        ),
                        'state': tfds.features.Tensor(
                            shape=(3,),
                            dtype=np.float64,
                            doc='Robot state, consists of [2x position, 1x yaw]',
                        ),
                        'position': tfds.features.Tensor(
                            shape=(2,),
                            dtype=np.float64,
                            doc='Robot position',
                        ),
                        'yaw': tfds.features.Tensor(
                            shape=(1,),
                            dtype=np.float64,
                            doc='Robot yaw',
                        ),
                        'yaw_rotmat': tfds.features.Tensor(
                            shape=(3, 3),
                            dtype=np.float64,
                            doc='Robot yaw rotation matrix',
                        ),

                    }),
                    'action': tfds.features.Tensor(
                        shape=(2,),
                        dtype=np.float64,
                        doc='Robot action, consists of 2x position'
                    ),
                     'action_angle': tfds.features.Tensor(
                        shape=(3,),
                        dtype=np.float64,
                        doc='Robot action, consists of 2x position, 1x yaw',
                    ),

                    'discount': tfds.features.Scalar(
                        dtype=np.float64,
                        doc='Discount if provided, default to 1.'
                    ),
                    'reward': tfds.features.Scalar(
                        dtype=np.float64,
                        doc='Reward if provided, 1 on final step for demos.'
                    ),
                    'is_first': tfds.features.Scalar(
                        dtype=np.bool_,
                        doc='True on first step of the episode.'
                    ),
                    'is_last': tfds.features.Scalar(
                        dtype=np.bool_,
                        doc='True on last step of the episode.'
                    ),
                    'is_terminal': tfds.features.Scalar(
                        dtype=np.bool_,
                        doc='True on last step of the episode if it is a terminal step, True for demos.'
                    ),
                    'language_instruction': tfds.features.Tensor(
                        shape=(10,),
                        dtype=tf.string,
                        doc='Language Instruction.'
                    ),
                })
    breakpoint()
    new_traj["steps"] = tfds.features.Dataset(steps)
    new_traj["episode_metadata"] = tfds.features.FeaturesDict(traj["traj_metadata"]["episode_metadata"])

    return tfds.features.FeaturesDict(new_traj)
        

def main(args):

    # Load in the dataset
    data_dir = args.data_dir
    name = args.dataset_name
    builder = tfds.builder(name, data_dir=data_dir)
    dataset = dl.DLataset.from_rlds(builder, split="all", shuffle=False)
    breakpoint()
    resize_size = (128, 128)
    num_parallel_calls = tf.data.AUTOTUNE

    # Load the dataset traj and yaw files
    traj_infos = {}
    for dataset_name in DATASETS:
        traj_info_file = f"traj_info/{dataset_name}.pkl"
        with open(traj_info_file, "rb") as f:
            traj_info = pickle.load(f)
        traj_infos.update(traj_info)

    # decode + resize images (and depth images)
    dataset = dataset.frame_map(
        partial(
            apply_obs_transform,
            decode,
        ),
        num_parallel_calls,
    )

    # Fix the dataset
    dataset = dataset.traj_map(partial(fix_dataset, traj_info=traj_infos), num_parallel_calls=num_parallel_calls)
    
    # Remove the image_decoded field 
    dataset = dataset.traj_map(lambda traj: {k: v for k, v in traj.items() if k != "observation_decoded"}, num_parallel_calls=num_parallel_calls)

    # Write dataset as RLDS
    dataset = dataset.traj_map(reorganize_traj, num_parallel_calls=num_parallel_calls)
    dataset.save(args.output_dir)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data_dir", type=str, required=True)
    parser.add_argument("--dataset_name", type=str, required=True)
    parser.add_argument("--output_dir", type=str, default="gs://vlm-guidance-data/test")
    args = parser.parse_args()
    main(args)