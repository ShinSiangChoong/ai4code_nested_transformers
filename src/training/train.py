# Python stdlib
from pathlib import Path
import argparse
import os
import json
# General DS
import numpy as np
import pandas as pd
import wandb
# Torch
import torch
# Hugging Face
from transformers import AdamW, get_linear_schedule_with_warmup


from src.data.quickdata import get_dl
from src.utils import nice_pbar, make_folder, lr_to_4sf
from src.eval.metrics import kendall_tau
from src.models.baseline import MarkdownModel, NotebookModel
from src.data import read_data


def parse_args():
    parser = argparse.ArgumentParser(description='Process some arguments')
    parser.add_argument('--model_name_or_path', type=str, default='microsoft/codebert-base')
    # parser.add_argument('--train_mark_path', type=str, default='./data/train_mark.csv')
    # parser.add_argument('--train_features_path', type=str, default='./data/train_fts.json')
    # parser.add_argument('--val_mark_path', type=str, default='./data/val_mark.csv')
    # parser.add_argument('--val_features_path', type=str, default='./data/val_fts.json')
    # parser.add_argument('--val_path', type=str, default="./data/val.csv")

    parser.add_argument('--max_n_cells', type=int, default=128)
    parser.add_argument('--max_len', type=int, default=64)
    
    parser.add_argument('--batch_size', type=int, default=1)
    parser.add_argument('--accumulation_steps', type=int, default=4)
    parser.add_argument('--epochs', type=int, default=5)
    parser.add_argument('--n_workers', type=int, default=1)

    parser.add_argument('--wandb_mode', type=str, default="offline")
    parser.add_argument('--output_dir', type=str, default="./outputs")

    args = parser.parse_args()

    PROC_DIR = Path(os.environ['PROC_DIR'])
    args.nb_meta_path = PROC_DIR / "nb_meta.json"
    args.cells_path = PROC_DIR / "cells.pkl"
    args.train_id_path = PROC_DIR / "train_id.pkl"
    args.val_id_path = PROC_DIR / "val_id.pkl"
    args.val_path = PROC_DIR / "val.csv"
    return args


def train(model, train_loader, val_loader, epochs):
    np.random.seed(0)

    # TODO: Refactor to data
    DATA_DIR = Path(os.environ['RAW_DIR'])
    PROC_DIR = Path(os.environ['PROC_DIR'])
    val_ids = pd.read_pickle(PROC_DIR / 'val_id.pkl')
    val_df = pd.read_pickle(PROC_DIR / 'cells.pkl')
    val_df = val_df.loc[val_ids.tolist()]
    nb_meta = json.load(open(PROC_DIR / "nb_meta.json", "rt"))

    df_orders = pd.read_csv(
        DATA_DIR / 'train_orders.csv',
        index_col='id',
        squeeze=True,
    ).str.split()
    
    # Creating optimizer and lr schedulers
    param_optimizer = list(model.named_parameters())
    no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay': 0.01},
        {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
    ]

    num_train_optimization_steps = int(args.epochs * len(train_loader) / args.accumulation_steps)
    optimizer = AdamW(optimizer_grouped_parameters, lr=3e-5,
                      correct_bias=False)  # To reproduce BertAdam specific behavior set correct_bias=False
    scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps=0.05 * num_train_optimization_steps,
                                                num_training_steps=num_train_optimization_steps)  # PyTorch scheduler

    criterion = torch.nn.CrossEntropyLoss(reduction='none')
    # criterion = torch.nn.L1Loss(reduction='none')
    scaler = torch.cuda.amp.GradScaler()

    
    from src.eval import get_preds
    # baseline score
    preds = val_df.groupby('id')['cell_id'].apply(list)
    print('baseline score:', kendall_tau(df_orders.loc[preds.index], preds))

    # initial score
    # preds = get_preds(model, val_loader, val_df, nb_meta)
    # print('initial score:', kendall_tau(df_orders.loc[preds.index], preds))

    for epoch in range(1, epochs + 1):
        model.train()
        loss_list = []
        # preds = []
        # labels = []

        tbar = nice_pbar(train_loader, len(train_loader), f"Train {epoch}")

        for idx, d in enumerate(tbar):
            for k in d:
                if k != 'nb_ids':
                    d[k] = d[k].cuda()
            with torch.cuda.amp.autocast():
                pred = model(
                    d['tokens'], 
                    d['cell_masks'], 
                    d['nb_masks'], 
                    d['label_masks'],
                    d['pos'],
                    d['start'], 
                    d['md_pct']
                ).masked_fill(d['label_masks'], -6.5e4)
                loss = criterion(pred.permute(0, 2, 1), d['labels']) * d['nb_masks'][:, :-1]
                loss = loss.sum() / d['nb_masks'][:, :-1].sum()
                # loss = (loss*d['loss_ws']).sum() / d['loss_ws'].sum()
            scaler.scale(loss).backward()

            if idx % args.accumulation_steps == 0 or idx == len(tbar) - 1:
                scaler.step(optimizer)
                scaler.update()
                optimizer.zero_grad()
                scheduler.step()

            loss_list.append(loss.item())
            # preds.append(pred.detach().numpy().ravel())
            # labels.append(target.detach().numpy().ravel())

            if idx % 20 == 0:
                metrics = {}
                metrics['loss'] = np.round(np.mean(loss_list[-20:]), 4)
                metrics['prev_lr'], metrics['next_lr'] = scheduler.get_last_lr()
                metrics['diff_lr'] = metrics['next_lr'] - metrics['prev_lr']
                wandb.log(metrics)

                tbar.set_postfix(loss=metrics['loss'], lr=lr_to_4sf(scheduler.get_last_lr()))

            if scheduler.get_last_lr()[0] == 0:
                break
        torch.save(model.state_dict(), f"{args.output_dir}/model-{epoch}.bin")

        # TODO: Refactor to eval
        from src.eval import get_preds
        preds = get_preds(model, val_loader, val_df, nb_meta)

        metrics = {}
        metrics['pred_score'] = kendall_tau(df_orders.loc[preds.index], preds)
        metrics['avg_train_loss'] = np.mean(loss_list)
        wandb.log(metrics)
        print("Preds score", metrics['pred_score'])
        print("Avg train loss", metrics['avg_train_loss'])
        if scheduler.get_last_lr()[0] == 0:
            break
        
    return model, preds


def main(args):
    make_folder(args.output_dir)

    train_loader = get_dl(is_train=True, args=args)
    val_loader = get_dl(is_train=False, args=args)

    model = NotebookModel(args.model_name_or_path)
    model = model.cuda()
    wandb.watch(model, log_freq=10000, log_graph=True, log="all")

    # Tracking dump
    print(model)
    print(os.environ)
    print(args)

    model, y_pred = train(model, train_loader, val_loader, epochs=args.epochs)


if __name__ == '__main__':
    args = parse_args()
    wandb.init(
        project="AI4Code Dev",
        name="Baseline",
        mode=args.wandb_mode,
    )
    try:
        main(args)
    finally:
        wandb.finish()
