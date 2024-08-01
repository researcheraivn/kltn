import collections
import json
import os
# os.environ["CUDA_VISIBLE_DEVICES"]='7'
from sys import path
path.append(os.getcwd())
# path.append('/home/Medical_Understanding/MSL')
from typing import List
import time
import heapq
from tqdm import tqdm

import argparse
import glob
import logging
import math
import numpy as np
import copy
import torch
import transformers as tfs
from transformers import BertTokenizer, T5ForConditionalGeneration, Text2TextGenerationPipeline

from data_utils import data_collator, reader_dataset
from data_utils import utils as du
from data_utils import read_json, write_json
from utils import checkpoint
from utils import dist_utils
from utils import model_utils
from utils import options
from utils import sampler
from utils import utils
from preprocess_data import DatasetReader
from tensorboardX import SummaryWriter

try:
    from apex import amp
except:
    pass

logger = logging.getLogger()
logger.setLevel(logging.INFO)
if logger.hasHandlers():
    logger.handlers.clear()
console = logging.StreamHandler()
logger.addHandler(console)


class ModelTrainer(object):

    def __init__(self, args):
        logger.info('Initializing components for training')
        utils.print_section_bar('Initializing components for training')

        logger.info("Loading tokenizer...")
        tokenizer = BertTokenizer.from_pretrained(args.pretrained_model_cfg)
        tokenizer.add_special_tokens({'additional_special_tokens': ['possible_statues']})
        logger.info("Tokenizer loaded and special tokens added.")

        logger.info("Loading model configuration...")
        cfg = tfs.T5Config.from_pretrained(args.pretrained_model_cfg)
        model = T5ForConditionalGeneration.from_pretrained(args.pretrained_model_cfg)
        if cfg.vocab_size != len(tokenizer):
            logger.info(f"Resizing embedding from {cfg.vocab_size} to {len(tokenizer)}")
            model.resize_token_embeddings(len(tokenizer))

        logger.info(f"Loading model state from {args.model_recover_path}...")
        model.load_state_dict(torch.load(args.model_recover_path, map_location=args.device))
        model.to(args.device)
        logger.info("Model loaded and moved to device.")

        self.model = model
        self.args = args
        self.tokenizer = tokenizer

    def get_eval_data_loader(self, eval_dataset):
        logger.info("Creating evaluation data loader...")
        if torch.distributed.is_initialized():
            eval_sampler = sampler.SequentialDistributedSampler(
                eval_dataset,
                num_replicas=self.args.distributed_world_size,
                rank=self.args.local_rank)
        else:
            assert self.args.local_rank == -1
            eval_sampler = torch.utils.data.SequentialSampler(eval_dataset)

        dataloader = torch.utils.data.DataLoader(
            eval_dataset,
            batch_size=self.args.dev_batch_size,
            pin_memory=True,
            sampler=eval_sampler,
            num_workers=0,
            collate_fn=data_collator.collate_fn,
            drop_last=False)
        logger.info("Evaluation data loader created.")
        return dataloader

    def validate(self):
        logger.info("Starting validation...")
        args = self.args

        logger.info(f"Loading evaluation dataset from {args.dev_file}...")
        eval_dataset = reader_dataset.ReaderMedDataset_gen(args.dev_file, self.tokenizer, stage='stage1')
        eval_dataloader = self.get_eval_data_loader(eval_dataset)

        all_results = []
        for step, batch in enumerate(eval_dataloader):
            self.model.eval()
            batch = tuple(t.to(args.device) for t in batch)
            input_ids, input_masks, labels, dial_id, window_id, term_id = batch
            logger.info(f"Processing batch {step + 1}/{len(eval_dataloader)}")

            if args.local_rank != -1:
                with self.model.no_sync(), torch.no_grad():
                    outputs = self.model.generate(inputs=input_ids, attention_mask=input_masks, max_length=64)
            else:
                with torch.no_grad():
                    outputs = self.model.generate(input_ids, attention_mask=input_masks, max_length=64, num_beams=1, num_return_sequences=1)
            logger.info(f"Generated outputs for batch {step + 1}")

            generated_texts = self.tokenizer.batch_decode(outputs, skip_special_tokens=True)
            dial_id = list(dial_id.detach().cpu())
            window_id = list(window_id.detach().cpu())
            term_id = list(term_id.detach().cpu())
            all_results.extend(zip(generated_texts, dial_id, window_id, term_id))

        logger.info("Validation completed.")
        return all_results


def post_process(predicts, data_dir):
    predict_ids = []
    reader = DatasetReader(data_dir=data_dir)
    all_terms = list(reader.term_ids.keys())
    for predict in predicts:
        generated_text, dial_id, window_id, _ = predict
        dial_id = int(dial_id)
        window_id = int(window_id)

        # get predicted term
        generated_terms = ''.join(generated_text.split(' ')).split('，')
        generated_term_list = []
        for term in generated_terms:
            if term in all_terms:
                generated_term_list.append(term)
            else:
                for term_ in all_terms:
                    if term_ in term:
                        generated_term_list.append(term_)
        generated_term_list = list(set(generated_term_list))
        generated_term_id_list = [reader.term_ids[term] for term in generated_term_list]
        for term_id in generated_term_id_list:
            predict_ids.append([dial_id, window_id, int(term_id)])
    return predict_ids

def main():
    parser = argparse.ArgumentParser()

    options.add_model_params(parser)
    options.add_cuda_params(parser)
    options.add_training_params(parser)
    options.add_data_params(parser)
    args = parser.parse_args()

    logger.info("Setting up directories...")
    # Ensure args.data_dir and args.output_dir are set correctly
    args.data_dir = os.path.join(args.origin_data_dir, args.data_dir)
    if args.add_category:
        args.data_dir += '_add_category'
        args.output_dir += '_add_category'
    if args.add_state:
        args.data_dir += '_add_state'
        args.output_dir += '_add_state'

    # Ensure model_recover_dir and log_dir are set correctly
    args.model_recover_dir = os.path.join(args.output_dir, args.model_recover_dir)
    args.log_dir = os.path.join(args.output_dir, args.log_dir)

    # Ensure train_file and dev_file are set correctly
    args.train_file = os.path.join(args.data_dir, args.train_file)
    args.dev_file = os.path.join(args.data_dir, args.dev_file)

    # Check if args.knowledge_file is None and handle it
    if args.knowledge_file:
        args.knowledge_file = os.path.join(args.origin_data_dir, args.knowledge_file)
    else:
        logger.warning("The argument 'knowledge_file' is required but not set.")

    assert os.path.exists(args.pretrained_model_cfg), \
        (f'{args.pretrained_model_cfg} doesn\'t exist. '
         f'Please manually download the HuggingFace model.')

    options.setup_args_gpu(args)
    options.set_seed(args)

    if dist_utils.is_local_master():
        utils.print_args(args)

    mode = args.dev_file.split('/')[-1].split('.')[0]
    model_dir = args.model_recover_dir.split('/model')[0]

    for i in range(0, 4):
        logger.info(f"Starting validation for model iteration {i}...")
        args.model_recover_path = args.model_recover_dir.format(i)
        trainer = ModelTrainer(args)
        predicts = trainer.validate()
        logger.info(f"Validation complete for model iteration {i}.")

        predict_ids = post_process(predicts, data_dir=args.origin_data_dir)
        output_path = os.path.join(model_dir, 'pred_stage1_{}_{}.json'.format(mode, i))
        logger.info(f"Writing predictions to {output_path}...")
        write_json(data=predict_ids, path=output_path)
        logger.info(f"Finished writing predictions for model iteration {i}.")


if __name__ == '__main__':
    main()