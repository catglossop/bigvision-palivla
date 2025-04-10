import tensorflow as tf 
import tensorflow_datasets as tfds
import numpy as np
import matplotlib.pyplot as plt
import os
import glob 
import pickle 
import argparse
import sys
import tqdm
import dlimp as dl
from functools import partial
from typing import Callable, Mapping, Optional, Sequence, Tuple, Union
import multiprocessing as mp
import os.path as osp
import traceback
from tqdm_multiprocess import TqdmMultiProcessPool
# import tyro

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
def fix_traj(traj, frames, episode_metadata, traj_info):
    
    # Get the metadata for this traj 
    traj_name = episode_metadata["file_path"].decode("utf-8").split("/")[-1]
    traj_base_name = traj_name.split("_chunk_")[0]
    traj_start = int(traj_name.split("_start_")[-1].split("_")[0])
    traj_end = int(traj_name.split("_end_")[-1].split("_")[0])

    # Modify the traj info for this trajectory
    curr_traj_info = traj_info[traj_base_name]

    # Check the number of non-white images in the traj
    image_non_white = np.sum(np.any(frames != 255, axis=-1), axis=(1, 2)) > 0
    num_non_white = np.sum(image_non_white)

    # Check two things: 
    # 1. Is the spacing between points close to that of the expected normalization factor
    # 2. Modify the yaw such that is closer to the original traj yaw

    # Check the spacing between points
    traj_pos = traj["observation"]["position"]
    deltas = np.linalg.norm(traj_pos[:-1] - traj_pos[1:], axis=-1)
    spacing = np.mean(deltas)
    normalization_factor = curr_traj_info["normalization_factor"]
    if np.abs(spacing - normalization_factor) > 0.5:
        print(f"Spacing issue for {traj_base_name} with spacing {spacing} and normalization factor {normalization_factor}")
    
    # Check the yaw
    traj_yaw = traj["observation"]["yaw"]
    non_cf_yaw = tf.squeeze(traj_yaw[:num_non_white], axis=-1).numpy()
    orig_yaw = curr_traj_info["yaw"]
    end = np.min((traj_start + num_non_white, traj_end))
    curr_orig_yaw = orig_yaw[traj_start:end].squeeze()
    if len(curr_orig_yaw.shape) < 1:
        curr_orig_yaw = np.expand_dims(curr_orig_yaw, 0)
    try:
        assert non_cf_yaw.shape == curr_orig_yaw.shape, f"Non cf yaw shape {non_cf_yaw.shape} does not match orig yaw shape {curr_orig_yaw.shape}"
    except:
        breakpoint()

    # If the trajectory has a counterfactual, we need to generate the correct yaw for the counterfactual part
    if "cf" in traj_name and num_non_white < traj_pos.shape[0] - 1:
        cf_start = num_non_white
        cf_new = np.arctan2(traj_pos[cf_start+1:, 1] - traj_pos[cf_start:-1, 1], traj_pos[cf_start+1:, 0] - traj_pos[cf_start:-1, 0])
        cf_new = cf_new - cf_new[0] + curr_orig_yaw[-1]
        assert (curr_orig_yaw[-1] - cf_new[0]) < 0.5, f"Yaw difference between orig and cf {curr_orig_yaw[-1] - cf_new[0]}"
        new_yaw = np.expand_dims(np.concatenate([curr_orig_yaw, cf_new, cf_new[[-1]]], axis=0), 1)
    elif "cf" in traj_name and num_non_white >= traj_pos.shape[0] - 1:
        new_yaw = np.expand_dims(curr_orig_yaw, 1)
        if new_yaw.shape[0] < traj_pos.shape[0]:
            new_yaw = np.concatenate([new_yaw, new_yaw[[-1]]], axis=0)
    else:
        new_yaw = orig_yaw[traj_start:end+1]
        if new_yaw.shape[0] > traj_pos.shape[0]:
            new_yaw = new_yaw[:-1]
        if len(new_yaw.shape) < 2:
            new_yaw = np.expand_dims(new_yaw, 1)
    
    assert new_yaw.shape == traj_yaw.shape, f"New yaw shape {new_yaw.shape} does not match traj yaw shape {traj_yaw.shape}, positions shape {traj_pos.shape}, curr orig yaw shape {curr_orig_yaw.shape}"

    traj["observation"]["yaw"] = new_yaw
    traj["observation"]["yaw_rotmat"] =  np.stack([np.cos(new_yaw), -np.sin(new_yaw), np.zeros(new_yaw.shape), np.sin(new_yaw), np.cos(new_yaw), np.zeros(new_yaw.shape), np.zeros(new_yaw.shape), np.zeros(new_yaw.shape), np.ones(new_yaw.shape)], axis=-1)
    traj["observation"]["yaw_rotmat"] = traj["observation"]["yaw_rotmat"].reshape(-1, 3, 3)

    return traj, normalization_factor

def work_fn(worker_id, path_shards, output_dir, traj_infos, old_features, new_features, tqdm_func=None, global_tqdm=None):
    print(f"Worker {worker_id} starting")
    print(output_dir)
    # paths = path_shards[worker_id]
    for path in path_shards:

        if osp.join(output_dir, osp.basename(path)) in tf.io.gfile.glob(f"{output_dir}/*.tfrecord*"):
            continue

        writer = tf.io.TFRecordWriter(osp.join(output_dir, osp.basename(path)))
        dataset = tf.data.TFRecordDataset([path]).map(old_features.deserialize_example)
        for example in dataset:

            traj = example["steps"].batch(int(1e9)).get_single_element()
            del example["steps"]

            example = tf.nest.map_structure(lambda x: x.numpy(), example)
            traj = tf.nest.map_structure(lambda x: x.numpy(), traj)
            frames = traj["observation"]["image"]
            episode_metadata = example["episode_metadata"]

            traj, normalization_factor = fix_traj(traj, frames, episode_metadata, traj_infos)
            
            # serialize and write
            example["steps"] = traj
            example["episode_metadata"]["normalization_factor"] = normalization_factor

            writer.write(new_features.serialize_example(example))

        global_tqdm.update(1)
        writer.close()

def main(args):

    # Load in the dataset
    data_dir = args.data_dir
    name = args.dataset_name
    output_dir = args.output_dir
    num_workers = args.num_workers

    builder = tfds.builder(name, data_dir=data_dir)
    output_dir = osp.join(output_dir, *str(builder.data_path).split(osp.sep)[-2:])
    tf.io.gfile.makedirs(output_dir)
    paths = tf.io.gfile.glob(f"{builder.data_path}/*.tfrecord*")
    path_shards = np.array_split(paths, num_workers)
    
    num_parallel_calls = tf.data.AUTOTUNE

    # Load the dataset traj and yaw files
    traj_infos = {}
    for dataset_name in DATASETS:
        traj_info_file = f"traj_info/{dataset_name}.pkl"
        with open(traj_info_file, "rb") as f:
            traj_info = pickle.load(f)
        traj_infos.update(traj_info)

    # Write dataset as RLDS
    old_features = builder.info.features
    new_features = new_features = tfds.features.FeaturesDict(
        {
            **builder.info.features,
            "episode_metadata": tfds.features.FeaturesDict(
                { **builder.info.features["episode_metadata"],
                **{"normalization_factor" : tfds.features.Tensor(shape=(), dtype=tf.float32)},
                }
            ),
        }
    )
    builder.info._features = new_features
    builder.info.write_to_directory(output_dir)
    
    if num_workers == 1:
        worker_id = 0
        work_fn(worker_id, path_shards[0], output_dir, traj_infos, old_features, new_features)
    else:
        tasks = [(work_fn, (i, path_shards[i], output_dir, traj_infos, old_features, new_features)) for i in range(args.num_workers)]
        pool = TqdmMultiProcessPool(args.num_workers)
        print("Starting multiprocessing")
        with tqdm.tqdm(total=len(paths), 
                dynamic_ncols=True,
                position=0,
                desc="Total progress"
        ) as pbar:
            pool.map(pbar, tasks, lambda _: None, lambda _: None)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data_dir", type=str, required=True)
    parser.add_argument("--dataset_name", type=str, required=True)
    parser.add_argument("--output_dir", type=str, default="gs://cat-datasets/cleaned")
    parser.add_argument("--num_workers", type=int, default=1)
    args = parser.parse_args()
    main(args)