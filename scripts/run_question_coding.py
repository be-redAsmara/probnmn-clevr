import argparse
import copy
from datetime import datetime
import itertools
import json
import os
import random
from typing import Dict, Optional, Union

from allennlp.data import Vocabulary
from allennlp.nn import util as allennlp_util
from tensorboardX import SummaryWriter
import torch
from torch import nn, optim
from torch.utils.data import DataLoader
from tqdm import tqdm
import yaml

from tbd.data import QuestionCodingDataset
from tbd.data.sampler import SupervisionWeightedRandomSampler
from tbd.models import ProgramPrior, ProgramGenerator, QuestionReconstructor
from tbd.opts import add_common_opts
from tbd.utils.checkpointing import CheckpointManager


parser = argparse.ArgumentParser("Question coding for CLEVR v1.0 programs and questions.")
parser.add_argument(
    "--config-yml",
    default="configs/question_coding.yml",
    help="Path to a config file listing model and solver parameters.",
)
# data file paths, gpu ids, checkpoint args etc.
add_common_opts(parser)

# for reproducibility - refer https://pytorch.org/docs/stable/notes/randomness.html
torch.manual_seed(0)
torch.cuda.manual_seed_all(0)
torch.backends.cudnn.benchmark = False
torch.backends.cudnn.deterministic = True


def do_iteration(config: Dict[str, Union[int, float, str]],
                 batch: Dict[str, torch.Tensor],
                 program_prior: ProgramPrior,
                 program_generator: ProgramGenerator,
                 question_reconstructor: QuestionReconstructor,
                 optimizer: Optional[optim.Optimizer] = None):
    """Perform one iteration - forward, backward passes (and optim step, if training)."""

    # program_generator and question_reconstructor would simultaneusly be in either training
    # mode or evaluation mode. program_prior is always in evaluation mode (frozen checkpoint).
    if program_generator.training:
        optimizer.zero_grad()

    # keys: {"predictions", "loss"}
    __pg_output_dict = program_generator(batch["question"], batch["program"])
    # shape: (batch_size, max_program_length)
    sampled_programs = program_generator(batch["question"], greedy_decode=True)["predictions"]
    # keys: {"predictions", "loss"}
    __qr_output_dict = question_reconstructor(sampled_programs, batch["question"])

    __qr_batch_loss = torch.mean(__qr_output_dict["loss"])
    # Zero out loss contribution from examples having no program supervision.
    __pg_batch_loss = allennlp_util.masked_mean(
        __pg_output_dict["loss"], batch["supervision"], dim=0
    )

    # Question coding objective: (reconstruction loss) + (alpha * supervision loss)
    # Reconstruction loss:       \log{p_\theta (x|z)} 
    # Supervision loss:          \log{r_\phi (z|x)} * [x \in S]
    loss_objective = __qr_batch_loss + config["ssl_alpha"] * __pg_batch_loss

    if program_generator.training and question_reconstructor.training:
        loss_objective.backward()
        optimizer.step()

    return {
        "predictions": {
            "__pg": __pg_output_dict["predictions"],
            "__qr": __qr_output_dict["predictions"]
        },
        "loss": {
            "reconstruction": __pg_batch_loss,
            "supervision": __qr_batch_loss,
            "objective": loss_objective
        }
    }


if __name__ == "__main__":
    # ============================================================================================
    #   INPUT ARGUMENTS AND CONFIG
    # ============================================================================================
    args = parser.parse_args()
    config = yaml.load(open(args.config_yml))
    device = torch.device("cuda", args.gpu_ids[0]) if args.gpu_ids[0] >= 0 else torch.device("cpu")

    # print config and args
    print(yaml.dump(config, default_flow_style=False))
    for arg in vars(args):
        print("{:<20}: {}".format(arg, getattr(args, arg)))

    # ============================================================================================
    #   SETUP VOCABULARY, DATASET, DATALOADER, MODEL, OPTIMIZER
    # ============================================================================================

    vocabulary = Vocabulary.from_files(args.vocab_dirpath)
    train_dataset = QuestionCodingDataset(
        args.tokens_train_h5, config["num_supervision"]
    )
    val_dataset = QuestionCodingDataset(args.tokens_val_h5)
    train_sampler = SupervisionWeightedRandomSampler(train_dataset)

    # Train dataloader can be re-initialized later while doing batch size scheduling.
    batch_size = config["initial_bs"]
    train_dataloader = DataLoader(train_dataset, batch_size=batch_size, sampler=train_sampler)
    val_dataloader = DataLoader(val_dataset, batch_size=batch_size)

    # Program Prior checkpoint, this will be frozen during question coding.
    program_prior = ProgramPrior(
        vocabulary=vocabulary,
        embedding_size=config["prior_embedding_size"],
        hidden_size=config["prior_rnn_hidden_size"],
        dropout=config["prior_rnn_dropout"]
    ).to(device)
    program_prior.load_state_dict(torch.load(config["prior_checkpoint"]))
    program_prior.eval()

    # Works with 100% supervision for now.
    program_generator = ProgramGenerator(
        vocabulary=vocabulary,
        embedding_size=config["embedding_size"],
        hidden_size=config["rnn_hidden_size"],
        dropout=config["rnn_dropout"]
    ).to(device)

    question_reconstructor = QuestionReconstructor(
        vocabulary=vocabulary,
        embedding_size=config["embedding_size"],
        hidden_size=config["rnn_hidden_size"],
        dropout=config["rnn_dropout"]
    ).to(device)

    optimizer = optim.Adam(
        itertools.chain(program_generator.parameters(), question_reconstructor.parameters()),
        lr=config["initial_lr"], weight_decay=config["weight_decay"]
    )
    lr_scheduler = optim.lr_scheduler.MultiStepLR(
        optimizer, milestones=config["lr_steps"], gamma=config["lr_gamma"],
    )

    if -1 not in args.gpu_ids:
        # Don't wrap to DataParallel for CPU-mode.
        program_prior = nn.DataParallel(program_prior, args.gpu_ids)
        program_generator = nn.DataParallel(program_generator, args.gpu_ids)
        question_reconstructor = nn.DataParallel(question_reconstructor, args.gpu_ids)

    # ============================================================================================
    #   TRAINING LOOP
    # ============================================================================================
    program_generator_checkpoint_manager = CheckpointManager(
        checkpoint_dirpath=args.save_dirpath,
        config=config,
        model=program_generator,
        optimizer=optimizer,
        filename_prefix="program_generator",
    )
    question_reconstructor_checkpoint_manager = CheckpointManager(
        checkpoint_dirpath=args.save_dirpath,
        config=config,
        model=question_reconstructor,
        optimizer=optimizer,
        filename_prefix="question_reconstructor",
    )
    summary_writer = SummaryWriter(log_dir=os.path.join(args.save_dirpath, "tensorboard_logs"))

    # make train dataloader iteration cyclical (gets re-initialized for batch size scheduling)
    train_dataloader = itertools.cycle(train_dataloader)

    print(f"Training for {config['num_iterations']} iterations:")
    for iteration in tqdm(range(1, config["num_iterations"] + 1)):
        batch = next(train_dataloader)
        for key in batch:
            batch[key] = batch[key].to(device)
        # keys: {"predictions", "loss"}
        iteration_output_dict = do_iteration(
            config, batch, None, program_generator, question_reconstructor, optimizer
        )
        # Log losses and hyperparameters.
        summary_writer.add_scalars("loss", iteration_output_dict["loss"], iteration)
        summary_writer.add_scalar("schedulers/batch_size", batch_size, iteration)
        summary_writer.add_scalar("schedulers/lr", optimizer.param_groups[0]["lr"], iteration)

        # batch size and learning rate scheduling
        lr_scheduler.step()
        if iteration in config["bs_steps"]:
            batch_size *= config["bs_gamma"]
            train_dataloader = itertools.cycle(
                DataLoader(train_dataset, batch_size=batch_size, sampler=train_sampler)
            )

        # ========================================================================================
        #   VALIDATE AND PRINT FEW EXAMPLES
        # ========================================================================================
        if iteration % args.checkpoint_every == 0:
            print(f"Cross-validation after iteration {iteration}:")
            program_generator.eval()
            question_reconstructor.eval()
            for i, batch in enumerate(tqdm(val_dataloader)):
                for key in batch:
                    batch[key] = batch[key].to(device)
                with torch.no_grad():
                    __iteration_output_dict = do_iteration(
                        config, batch, None, program_generator, question_reconstructor
                    )

            # Print 10 qualitative examples from last batch.
            print(f"Qualitative examples after iteration {iteration}...")
            print("- " * 30)
            for j in range(min(len(batch["question"]), 20)):
                print("PROGRAM: " + " ".join(
                    [vocabulary.get_token_from_index(p_index.item(), "programs")
                     for p_index in batch["program"][j] if p_index != 0]
                ))

                print("SAMPLED PROGRAM: " + " ".join(
                    [vocabulary.get_token_from_index(p_index.item(), "programs")
                     for p_index in __iteration_output_dict["predictions"]["__pg"][j]
                     if p_index != 0]
                ))

                print("QUESTION: " + " ".join(
                    [vocabulary.get_token_from_index(q_index.item(), "questions")
                     for q_index in batch["question"][j] if q_index != 0]
                ))

                print("RECONST QUESTION: " + " ".join(
                    [vocabulary.get_token_from_index(q_index.item(), "questions")
                     for q_index in __iteration_output_dict["predictions"]["__qr"][j]
                     if q_index != 0]
                ))
                print("- " * 30)

            # Log BLEU score and perplexity of both program_generator and question_reconstructor.
            if isinstance(program_generator, nn.DataParallel):
                __pg_metrics = program_generator.module.get_metrics()
                __qr_metrics = question_reconstructor.module.get_metrics()
            else:
                __pg_metrics = program_generator.get_metrics()
                __qr_metrics = question_reconstructor.get_metrics()

            # Log three metrics to tensorboard.
            # keys: {"BLEU", "perplexity", "sequence_accuracy"}
            for metric_name in __pg_metrics:
                summary_writer.add_scalars(
                    "metrics/" + metric_name,
                    {
                        "program_generator": __pg_metrics[metric_name],
                        "question_reconstructor": __qr_metrics[metric_name]
                    },
                    iteration
                )
            program_generator_checkpoint_manager.step(__pg_metrics["perplexity"])
            question_reconstructor_checkpoint_manager.step(__qr_metrics["perplexity"])
            print("\n")
            program_generator.train()
            question_reconstructor.train()

    # ============================================================================================
    #   AFTER TRAINING END
    # ============================================================================================
    program_generator_checkpoint_manager.save_best()
    question_reconstructor_checkpoint_manager.save_best()
    summary_writer.close()
