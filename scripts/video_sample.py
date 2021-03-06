from ast import parse
from shutil import move
import torch
import numpy as np
from torch.utils.data import DataLoader
from argparse import ArgumentParser, Namespace
import os
from tqdm.auto import tqdm
from pathlib import Path
import json
from PIL import Image

from improved_diffusion.script_util import (
    video_model_and_diffusion_defaults,
    create_video_model_and_diffusion,
    args_to_dict,
)
from improved_diffusion import dist_util
from improved_diffusion.image_datasets import get_train_dataset, get_test_dataset, get_variable_length_dataset
from improved_diffusion.script_util import str2bool
from improved_diffusion import inference_util
from improved_diffusion import test_util


# A dictionary of default model configs for the parameters newly introduced.
# It enables backward compatibility
default_model_configs = {"enforce_position_invariance": False,
                         "cond_emb_type": "channel"}


def get_masks(x0, num_obs):
    """ Generates observation, latent, and kinda-marginal masks.
    Assumes that the first num_obs frames are observed. and the rest are latent.
    Also, assumes that no frames are kinda-marginal.

    Args:
        x0 (torch.FloatTesnor): The input video.
        num_obs (int): Number of observed frames.

    Returns:
        Observation, latent, and kina-marginal masks
    """
    obs_mask = torch.zeros_like(x0[:, :, :1, :1, :1])
    obs_mask[:, :num_obs] = 1
    latent_mask = 1 - obs_mask
    kinda_marg_mask = torch.zeros_like(x0[:, :, :1, :1, :1])
    return obs_mask, latent_mask, kinda_marg_mask


@torch.no_grad()
def infer_video(mode, model, diffusion, batch, max_frames, obs_length,
                step_size=1, optimal_schedule_path=None, *, use_gradient_method):
    """
    batch has a shape of BxTxCxHxW where
    B: batch size
    T: video length
    CxWxH: image size
    """
    B, T, C, H, W = batch.shape
    samples = torch.zeros_like(batch).cpu()
    samples[:, :obs_length] = batch[:, :obs_length]
    if 'goal-directed' in mode:
        samples[:, -5] = batch[:, -5]
    adaptive_kwargs = dict(distance='lpips') if 'adaptive' in mode else {}
    frame_indices_iterator = iter(inference_util.inference_strategies[mode](
        video_length=T, num_obs=obs_length,
        max_frames=max_frames, step_size=step_size,
        optimal_schedule_path=optimal_schedule_path,
        **adaptive_kwargs
    ))

    while True:
        if 'adaptive' in mode:
            frame_indices_iterator.set_videos(samples.to(batch.device))
        try:
            obs_frame_indices, latent_frame_indices = next(frame_indices_iterator)
        except StopIteration:
            break
        print(f"Conditioning on {sorted(obs_frame_indices)} frames, predicting {sorted(latent_frame_indices)}.\n")
        # Prepare network's input
        if 'adaptive' in mode:
            frame_indices = torch.cat([torch.tensor(obs_frame_indices), torch.tensor(latent_frame_indices)], dim=1)
            x0 = torch.stack([samples[i, fi] for i, fi in enumerate(frame_indices)], dim=0).clone()
            obs_mask, latent_mask, kinda_marg_mask = get_masks(x0, len(obs_frame_indices[0]))
            n_latent = len(latent_frame_indices[0])
        else:
            x0 = torch.cat([samples[:, obs_frame_indices], samples[:, latent_frame_indices]], dim=1).clone()
            frame_indices = torch.cat([torch.tensor(obs_frame_indices), torch.tensor(latent_frame_indices)], dim=0).repeat((B, 1))
            obs_mask, latent_mask, kinda_marg_mask = get_masks(x0, len(obs_frame_indices))
            n_latent = len(latent_frame_indices)
        # Prepare masks
        print(f"{'Frame indices':20}: {frame_indices[0].cpu().numpy()}.")
        print(f"{'Observation mask':20}: {obs_mask[0].cpu().int().numpy().squeeze()}")
        print(f"{'Latent mask':20}: {latent_mask[0].cpu().int().numpy().squeeze()}")
        print("-" * 40)
        # Move tensors to the correct device
        [x0, obs_mask, latent_mask, kinda_marg_mask, frame_indices] = [xyz.to(batch.device) for xyz in [x0, obs_mask, latent_mask, kinda_marg_mask, frame_indices]]
        # Run the network
        local_samples, attention_map = diffusion.p_sample_loop(
            model, x0.shape, clip_denoised=True,
            model_kwargs=dict(frame_indices=frame_indices,
                              x0=x0,
                              obs_mask=obs_mask,
                              latent_mask=latent_mask,
                              kinda_marg_mask=kinda_marg_mask,
                              ),
            latent_mask=latent_mask,
            return_attn_weights=False,
            use_gradient_method=use_gradient_method,
            )
        # Fill in the generated frames
        if 'adaptive' in mode:
            n_obs = len(obs_frame_indices[0])
            for i, li in enumerate(latent_frame_indices):
                samples[i, li] = local_samples[i, n_obs:].cpu()
        else:
            samples[:, latent_frame_indices] = local_samples[:, -n_latent:].cpu()
    return samples.numpy()


def main(args, model, diffusion, dataloader, use_gradient_method, dataset_indices=None):
    optimal_schedule_path = None if args.optimality is None else args.eval_dir / "optimal_schedule.pt"
    dataset_idx_translate = lambda idx: idx if dataset_indices is None else dataset_indices[idx]
    # Generate and store samples
    cnt = 0
    for batch, _ in tqdm(dataloader, leave=True):
        batch_size = len(batch)
        for sample_idx in range(args.num_samples) if args.sample_idx is None else [args.sample_idx]:
            output_filenames = [args.eval_dir / "samples" / f"sample_{dataset_idx_translate(cnt + i):04d}-{sample_idx}.npy" for i in range(batch_size)]
            todo = [not p.exists() for (i, p) in enumerate(output_filenames)] # Whether the file should be generated
            if not any(todo):
                print(f"Nothing to do for the batches {cnt} - {cnt + batch_size - 1}, sample #{sample_idx}.")
            else:
                if args.T is not None:
                    batch = batch[:, :args.T]
                batch = batch.to(args.device)
                recon = infer_video(mode=args.inference_mode, model=model, diffusion=diffusion,
                                    batch=batch, max_frames=args.max_frames, obs_length=args.obs_length,
                                    step_size=args.step_size, optimal_schedule_path=optimal_schedule_path,
                                    use_gradient_method=use_gradient_method)
                recon = (recon - drange[0]) / (drange[1] - drange[0])  * 255 # recon with pixel values in [0, 255]
                recon = recon.astype(np.uint8)
                for i in range(batch_size):
                    if todo[i]:
                        np.save(output_filenames[i], recon[i])
                        print(f"*** Saved {output_filenames[i]} ***")
                    else:
                        print(f"Skipped {output_filenames[i]}")
        cnt += batch_size


def visualise(args):
    optimal_schedule_path = None if getattr(args, "optimality", None) is None else args.eval_dir / "optimal_schedule.pt"
    if 'adaptive' in args.inference_mode:
        dataset_name = dist_util.load_state_dict(args.checkpoint_path, map_location="cpu")['config']['dataset']
        dataset = globals()[f"get_{args.dataset_partition}_dataset"](dataset_name=dataset_name, T=args.T)
        batch = next(iter(DataLoader(dataset, batch_size=args.batch_size, shuffle=False, drop_last=False)))[0]
        adaptive_kwargs = dict(distance='lpips')
    else:
        adaptive_kwargs = {}
    frame_indices_iterator = inference_util.inference_strategies[args.inference_mode](
        video_length=args.T, num_obs=args.obs_length, max_frames=args.max_frames, step_size=args.step_size,
        optimal_schedule_path=optimal_schedule_path,
        **adaptive_kwargs
    )

    def visualise_obs_lat_sequence(sequence, index, path):
        """ if index is None, expects sequence to be a list of tuples of form (list, list)
            if index is given, expects sequence to be a list of tuples of form (list of lists (from which index i is taken), list of lists (from which index i is taken))
        """
        vis = []
        exist_indices = list(range(args.obs_length))
        for obs_frame_indices, latent_frame_indices in sequence:
            if index is not None:
                obs_frame_indices, latent_frame_indices = obs_frame_indices[index], latent_frame_indices[index]
            exist_indices.extend(latent_frame_indices)
            if args.big_visualise:
                new_layer = torch.zeros((args.T, 3)).int()
                border_colour = torch.tensor([0, 0, 0]).int()
                not_sampled_colour = torch.tensor([255, 255, 255]).int()
                exist_colour = torch.tensor([50, 50, 50]).int()
                obs_colour = torch.tensor([50, 50, 255]).int()
                latent_colour = torch.tensor([255, 69, 0]).int()
                # not_sampled_colour = torch.tensor([255, 255, 255]).int()
                # exist_colour = torch.tensor([153, 153, 153]).int()
                # obs_colour = torch.tensor([55, 126, 184]).int()
                # latent_colour = torch.tensor([255, 127, 0]).int()
                new_layer = torch.zeros((args.T, 3)).int()
                new_layer[:, :] = not_sampled_colour
                new_layer[exist_indices, :] = exist_colour
                new_layer[obs_frame_indices, :] = obs_colour
                new_layer[latent_frame_indices, :] = latent_colour
                scale = 4
                new_layer = new_layer.repeat_interleave(scale+1, dim=0)
                new_layer[::(scale+1)] = border_colour
                new_layer = torch.cat([new_layer, new_layer[:1]], dim=0)
                vis.extend([new_layer.clone() for _ in range(scale+1)])
                vis[-1][:] = border_colour
            else:
                new_layer = torch.zeros((args.T, 3)).int()
                new_layer[exist_indices, 0] = 50
                new_layer[obs_frame_indices, 0] = 255
                new_layer[latent_frame_indices, 2] = 255
                vis.append(new_layer)
                vis.append(new_layer*0)
        vis = torch.stack([vis[-1], *vis])
        if index is not None:
            path = f"{path}_index-{index}"
        path = f"{path}.png"
        Image.fromarray(vis.numpy().astype(np.uint8)).save(path)
        print(f"Saved to {path}")

    if 'adaptive' in args.inference_mode:
        frame_indices_iterator.set_videos(batch)
    indices = list(frame_indices_iterator)
    path = f"visualisations/sample_vis_{args.inference_mode}"
    if args.obs_length == 0:
        path += "_uncond"
    if getattr(args, "optimality", None) is not None:
        path += "_optimal-" + args.optimality
    path += f"_T={args.T}_sampling_{args.step_size}_out_of_{args.max_frames}"
    if 'adaptive' in args.inference_mode:
        for i in range(len(batch)):
            visualise_obs_lat_sequence(indices, i, path)
    else:
        visualise_obs_lat_sequence(indices, None, path)


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("checkpoint_path", type=str)
    parser.add_argument("--batch_size", type=int, default=8)
    parser.add_argument("--eval_dir", default=None, help="Path to the evaluation directory for the given checkpoint. If None, defaults to resutls/<checkpoint_dir_subset>/<checkpoint_name>.")
    parser.add_argument("--dataset_partition", default="test", choices=["train", "test", "variable_length"])
    parser.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    # Inference arguments
    parser.add_argument("--use_gradient_method", action='store_true')
    parser.add_argument("--inference_mode", required=True, choices=inference_util.inference_strategies.keys())
    parser.add_argument("--max_frames", type=int, default=None,
                        help="Maximum number of video frames (observed or latent) allowed to pass to the model at once. Defaults to what the model was trained with.")
    parser.add_argument("--obs_length", type=int, default=36,
                        help="Number of observed frames. It will observe this many frames from the beginning of the video and predict the rest. Defaults to 36.")
    parser.add_argument("--step_size", type=int, default=1,
                        help="Number of frames to predict in each prediciton step. Defults to 1.")
    parser.add_argument("--indices", type=int, nargs="*", default=None,
                        help="If not None, only generate videos for the specified indices. Used for handling parallelization.")
    parser.add_argument("--use_ddim", type=str2bool, default=False)
    parser.add_argument("--timestep_respacing", type=str, default="")
    parser.add_argument("--T", type=int, default=None,
                        help="Length of the videos. If not specified, it will be inferred from the dataset.")
    parser.add_argument("--subset_size", type=int, default=None,
                        help="If not None, only use a subset of the dataset. Defaults to the whole dataset.")
    parser.add_argument("--num_samples", type=int, default=1, help="Number of samples to generate for each test video.")
    parser.add_argument("--sample_idx", type=int, default=None, help="Sampled images will have this specific index. Used for parallel sampling on multiple machines. If this argument is given, --num_samples is ignored.")
    parser.add_argument("--just_visualise", action='store_true', help="Make visualisation of sampling mode instead of doing it.")
    parser.add_argument("--big_visualise", action='store_true', help="Make visualisation big.")
    parser.add_argument("--optimality", type=str, default=None,
        choices=["linspace-t", "random-t",
                 "linspace-t-force-nearby", "random-t-force-nearby"],
                 help="Whcih optimality schedule to use for choosing observed frames. The optimal schedule should be generated before via video_optimal_schedule.py. Default is to not use any optimality.")
    args = parser.parse_args()

    if args.just_visualise and args.optimality is None:
        visualise(args)
        exit()
    drange = [-1, 1] # Range of the generated samples' pixel values

    # Load the checkpoint (state dictionary and config)
    data = dist_util.load_state_dict(args.checkpoint_path, map_location="cpu")
    state_dict = data["state_dict"]
    model_args = data["config"]
    model_args.update({"use_ddim": args.use_ddim,
                       "timestep_respacing": args.timestep_respacing})
    # Update model parameters, if needed, to enable backward compatibility
    for k, v in default_model_configs.items():
        if k not in model_args:
            model_args[k] = v
    model_args = Namespace(**model_args)
    # Load the model
    model, diffusion = create_video_model_and_diffusion(
        **args_to_dict(model_args, video_model_and_diffusion_defaults().keys())
    )
    model.load_state_dict(
            state_dict
        )
    model = model.to(args.device)
    model.eval()
    # Update max_frames if not set
    if args.max_frames is None:
        args.max_frames = model_args.max_frames
    print(f"max_frames = {args.max_frames}")
    # Load the dataset
    dataset = locals()[f"get_{args.dataset_partition}_dataset"](dataset_name=model_args.dataset, T=args.T)
    print(f"Dataset size = {len(dataset)}")
    # Prepare the indices
    if args.indices is None and "SLURM_ARRAY_TASK_ID" in os.environ:
        assert args.subset_size is None
        task_id = int(os.environ["SLURM_ARRAY_TASK_ID"])
        args.indices = list(range(task_id * args.batch_size, (task_id + 1) * args.batch_size))
        print(f"Only generating predictions for the batch #{task_id}.")
    elif args.subset_size is not None:
        #indices = np.random.RandomState(123).choice(len(dataset), args.subset_size, replace=False)
        print(f"Only generating predictions for the first {args.subset_size} videos of the dataset.")
        args.indices = list(range(args.subset_size))
    elif args.indices is None:
        args.indices = list(range(len(dataset)))
        print(f"Generating predictions for the whole dataset.")
    # Take a subset of the dataset according to the indices
    dataset = torch.utils.data.Subset(dataset, args.indices)
    print(f"Dataset size (after subsampling according to indices) = {len(dataset)}")
    # Prepare the dataloader
    dataloader = DataLoader(dataset, batch_size=args.batch_size, shuffle=False, drop_last=False)
    args.eval_dir = test_util.get_model_results_path(args) / test_util.get_eval_run_identifier(args)
    args.eval_dir = args.eval_dir
    if args.dataset_partition == "variable_length":
         args.eval_dir = args.eval_dir / "variable_length"
         if args.T is None:
             args.T = {'0': 268, '1': 431, '2': 948}[os.environ["SLURM_ARRAY_TASK_ID"]]
    (args.eval_dir / "samples").mkdir(parents=True, exist_ok=True)
    print(f"Saving samples to {args.eval_dir / 'samples'}")

    if args.T is None:
        args.T = dataset[0][0].shape[0]
        print(f"Using the dataset video length as the T value ({args.T}).")

    if args.just_visualise:
        visualise(args)
        exit()

    # Store model configs in a JSON file
    json_path = args.eval_dir / "model_config.json"
    if not json_path.exists():
        with test_util.Protect(json_path): # avoids race conditions
            with open(json_path, "w") as f:
                json.dump(vars(model_args), f, indent=4)
        print(f"Saved model config at {json_path}")

    # Generate the samples
    main(args, model, diffusion, dataloader, dataset_indices=args.indices, use_gradient_method=args.use_gradient_method)
