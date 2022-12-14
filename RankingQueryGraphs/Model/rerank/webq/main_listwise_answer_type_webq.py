from __future__ import absolute_import, division, print_function

import argparse
import csv
import logging
import os
import random
import sys
import numpy as np
import torch
import math
import pickle
import shutil
from torch.utils.data import (DataLoader, RandomSampler, SequentialSampler,
                              TensorDataset)
from torch.utils.data.distributed import DistributedSampler
from tqdm import tqdm, trange
from torch.nn import CrossEntropyLoss, MSELoss
from scipy.stats import pearsonr, spearmanr
from sklearn.metrics import matthews_corrcoef, f1_score
from argparse import ArgumentParser
from pytorch_pretrained_bert.file_utils import PYTORCH_PRETRAINED_BERT_CACHE, WEIGHTS_NAME, CONFIG_NAME
from pytorch_pretrained_bert.modeling import BertForSequenceClassification, BertConfig
from pytorch_pretrained_bert.tokenization import BertTokenizer
from pytorch_pretrained_bert.optimization import BertAdam, WarmupLinearSchedule

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.append(BASE_DIR)
from Model.common.InputExample import InputExample
from Model.cal_f1 import cal_f1, cal_f1_with_position
from Model.common.DataProcessor import DataProcessor
# from Model.common.BertEncoderX import BertFor2PairSequenceWithAnswerType
from Model.common.BertEncoderX import BertFor2PairSequenceWithAnswerTypeMidDim as BertFor2PairSequenceWithAnswerType
from Model.rerank.loss import crossEntropy

    

def main(fout_res, args: ArgumentParser):
    best_model_dir_name = ''
    processor = DataProcessor(args)
    device = torch.device("cuda", 0)
    shutil.copy(__file__, args.output_dir + __file__)
    merge_mode = ['listwise']
    # import pdb; pdb.set_trace()
    tokenizer = BertTokenizer.from_pretrained(args.bert_vocab, do_lower_case=args.do_lower_case)
    # 构建验证集数据  
    eval_examples = processor.get_dev_examples(args.data_dir)
    # import pdb; pdb.set_trace()   
    # eval_data = processor.convert_examples_to_features(eval_examples, tokenizer)
    eval_data = processor.convert_examples_to_features_with_answer_type(eval_examples, tokenizer)
    eval_data = processor.build_data_for_model(eval_data, tokenizer, device)
    train_examples = processor.get_train_examples(args.data_dir)
    num_train_optimization_steps = math.ceil(math.ceil(len(train_examples) / args.train_batch_size)\
                                        / args.gradient_accumulation_steps) * args.num_train_epochs    
    # import pdb; pdb.set_trace()   
    # Prepare model
    cache_dir = args.cache_dir if args.cache_dir else os.path.join(str(PYTORCH_PRETRAINED_BERT_CACHE))
    model = BertFor2PairSequenceWithAnswerType.from_pretrained(args.bert_model,cache_dir=cache_dir,num_labels=1)
    model.to(device)
    # Prepare optimizer
    param_optimizer = list(model.named_parameters())
    no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay': 0.01},
        {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
        ]
    optimizer = BertAdam(optimizer_grouped_parameters,
                            lr=args.learning_rate,
                            warmup=args.warmup_proportion,
                            t_total=num_train_optimization_steps)
    global_step = 0
    nb_tr_steps = 0
    tr_loss = 0
    # import pdb; pdb.set_trace()
    # **************************
    if args.do_train:   
        i_train_step = 0
        # train_data = processor.convert_examples_to_features(train_examples, tokenizer)
        train_data = processor.convert_examples_to_features_with_answer_type(train_examples, tokenizer)
        train_data = processor.build_data_for_model_train(train_data, tokenizer, device)
        dev_acc = 0.0
        for _ in trange(int(args.num_train_epochs), desc="Epoch"):
            # train_sampler = SequentialSampler(train_data)
            train_sampler = RandomSampler(train_data)
            train_dataloader = DataLoader(train_data, sampler=train_sampler, batch_size=args.train_batch_size)
            model.train()
            tr_loss = 0
            point_loss = 0
            pair_loss = 0
            list_loss = 0
            nb_tr_examples, nb_tr_steps = 0, 0
            n_batch_correct = 0
            len_train_data = 0
            crossLoss = torch.nn.CrossEntropyLoss()
            for step, batch in enumerate(tqdm(train_dataloader, desc="Iteration")):
                i_train_step += 1
                batch = tuple(t.to(device) for t in batch)
                input_ids, input_mask, segment_ids, label_ids, rels_ids = batch
                # import pdb; pdb.set_trace()
                input_ids = input_ids.to(device).view(-1, args.max_seq_length)
                input_mask = input_mask.to(device).view(-1, args.max_seq_length)
                segment_ids = segment_ids.to(device).view(-1, args.max_seq_length)
                label_ids = label_ids.to(device).view(-1)
                # define a new function to compute loss values for both output_modes
                # import pdb; pdb.set_trace()
                logits = model(input_ids, segment_ids, input_mask, labels=None)
                # import pdb;pdb.set_trace()
                loss_point = torch.tensor(0.0).to(device)
                loss_pair = 0.0
                loss_list = 0.0
                if 'listwise' in merge_mode:
                    label_ids = label_ids.view(-1,2)[:,0]
                    logits_que = torch.softmax(logits.view(-1, args.group_size), 1)
                    label_ids_que = label_ids.view(-1, args.group_size)
                    # import pdb; pdb.set_trace()
                    for i, que_item in enumerate(logits_que):
                        for j, item in enumerate(que_item):
                            if(label_ids_que[i][j] == 0):
                                if(item != 1):
                                    loss_list += torch.log(1 - item)
                            else:
                                if(item != 0):
                                    loss_list += torch.log(item)
                                # import pdb; pdb.set_trace()
                    loss_list = 0 - loss_list
                    list_loss += loss_list.item()
                # 计算评价函数
                true_pos = torch.max(logits.view(-1, args.group_size), 1)[1]
                label_ids_que = label_ids.view(-1, args.group_size)
                for i, item in enumerate(true_pos):
                    if(label_ids_que[i][item] == 1):
                        n_batch_correct += 1
                len_train_data += logits.view(-1, args.group_size).size(0) 
                try:
                    loss = loss_list
                    loss.backward()      
                except:
                    import pdb; pdb.set_trace()
                tr_loss += loss.item()
                if (step + 1) % args.gradient_accumulation_steps == 0:                   
                    optimizer.step()
                    optimizer.zero_grad()
                    global_step += 1    
            optimizer.step()
            optimizer.zero_grad()
            print('train_loss:', tr_loss)    
            fout_res.write('single loss:' + str(point_loss) + '\t' + str(pair_loss) + '\t' + str(list_loss) + '\n')  
            fout_res.write('train loss:' + str(tr_loss) + '\n')
            P_train = 1. * int(n_batch_correct) / len_train_data
            print("train_Accuracy-----------------------",P_train)
            fout_res.write('train accuracy:' + str(P_train) + '\n')
            F_dev = 0
            if args.do_eval:
                file_name1 = args.output_dir + 'prediction_valid'
                f_valid = open(file_name1, 'w', encoding='utf-8')
                # Run prediction for full data
                eval_sampler = SequentialSampler(eval_data)
                eval_dataloader = DataLoader(eval_data, sampler=eval_sampler, batch_size=args.eval_batch_size)
                model.eval()
                P_dev = 0
                for input_ids, input_mask, segment_ids, label_ids, rels_ids in tqdm(eval_dataloader, desc="Evaluating"):
                    input_ids = input_ids.to(device).view(-1, args.max_seq_length)
                    input_mask = input_mask.to(device).view(-1, args.max_seq_length)
                    segment_ids = segment_ids.to(device).view(-1, args.max_seq_length)
                    label_ids = label_ids.to(device).view(-1)
                    rels_ids = rels_ids.to(device).view(-1, 2)
                    with torch.no_grad():
                        logits = model(input_ids, segment_ids, input_mask, labels=None)    
                    # logits = torch.sigmoid(logits)
                    # import pdb; pdb.set_trace()  
                    for item in logits:
                        f_valid.write(str(float(item)) + '\n')
                f_valid.flush()
                p, r, F_dev = cal_f1_with_position(file_name1, args.data_dir + args.v_file_name, 'v', -3)
                fout_res.write(str(p) + '\t' + str(r) + '\t' + str(F_dev) + '\n')
                fout_res.flush()
            if(True):
                model_to_save = model.module if hasattr(model, 'module') else model  # Only save the model it-self
                output_dir = args.output_dir + str(P_train) + '_' + str(F_dev) + '_' + str(_)
                if not os.path.exists(output_dir):
                    os.makedirs(output_dir)
                if(F_dev > dev_acc):
                    best_model_dir_name = output_dir
                    dev_acc = F_dev
                    print(best_model_dir_name)
                    # If we save using the predefined names, we can load using `from_pretrained`
                    output_model_file = os.path.join(output_dir, WEIGHTS_NAME)
                    output_config_file = os.path.join(output_dir, CONFIG_NAME)
                    torch.save(model_to_save.state_dict(), output_model_file)
                    model_to_save.config.to_json_file(output_config_file)
                    tokenizer.save_vocabulary(output_dir)
    return best_model_dir_name

def test(best_model_dir_name, fout_res, args):
    print('测试选用的模型是', best_model_dir_name)
    fout_res.write('测试选用的模型是:' + best_model_dir_name + '\n')
    processor = DataProcessor(args)
    device = torch.device("cuda", 0)
    merge_mode = ['pairwise']
    tokenizer = BertTokenizer.from_pretrained(best_model_dir_name, do_lower_case=args.do_lower_case)
    cache_dir = args.cache_dir if args.cache_dir else os.path.join(str(PYTORCH_PRETRAINED_BERT_CACHE))
    model = BertFor2PairSequenceWithAnswerType.from_pretrained(best_model_dir_name,cache_dir=cache_dir,num_labels=1)
    model.to(device)
    # 构建验证集数据  
    eval_examples = processor.get_test_examples(args.data_dir)
    # import pdb; pdb.set_trace()   
    # eval_data = processor.convert_examples_to_features(eval_examples, tokenizer)
    eval_data = processor.convert_examples_to_features_with_answer_type(eval_examples, tokenizer)
    eval_data = processor.build_data_for_model(eval_data, tokenizer, device)
    # import pdb; pdb.set_trace()
    file_name1 = args.output_dir + 'prediction_test'
    f_valid = open(file_name1, 'w', encoding='utf-8')
    # Run prediction for full data
    eval_sampler = SequentialSampler(eval_data)
    eval_dataloader = DataLoader(eval_data, sampler=eval_sampler, batch_size=args.eval_batch_size)
    model.eval()
    P_dev = 0
    for input_ids, input_mask, segment_ids, label_ids, rels_ids in tqdm(eval_dataloader, desc="Evaluating"):
        input_ids = input_ids.to(device).view(-1, args.max_seq_length)
        input_mask = input_mask.to(device).view(-1, args.max_seq_length)
        segment_ids = segment_ids.to(device).view(-1, args.max_seq_length)
        label_ids = label_ids.to(device).view(-1)
        with torch.no_grad():
            logits = model(input_ids, segment_ids, input_mask, labels=None)    
        # logits = torch.sigmoid(logits)
        # import pdb; pdb.set_trace()
        for item in logits:
            f_valid.write(str(float(item)) + '\n')
    f_valid.flush()
    p, r, F_dev = cal_f1_with_position(file_name1, args.data_dir + args.t_file_name, 't', -3)
    fout_res.write(str(p) + '\t' + str(r) + '\t' + str(F_dev) + '\n')
    fout_res.flush()

if __name__ == "__main__":
    seed = 42
    steps = 100
    # for N in [5, 10, 20, 30, 40, 50, 60, 70, 80, 100, 120, 140]:
    for N in [5, 10, 20]:
    # for N in [5]:
    # for N in [60, 80, 100]:
    # for N in [100, 80, 60, 50, 40, 30, 20]:
        logger = logging.getLogger(__name__)
        print(seed)
        os.environ["CUDA_VISIBLE_DEVICES"] = '4'
        parser = ArgumentParser(description = 'For KBQA')
        parser.add_argument("--data_dir",default=BASE_DIR + '/runnings/train_data/webq/',type=str)
        # parser.add_argument("--bert_model", default='bert-base-uncased', type=str)
        # parser.add_argument("--bert_vocab", default='bert-base-uncased', type=str)
        parser.add_argument("--bert_model", default=BASE_DIR + '/data/pretrain_model/bert_base_uncased', type=str)
        parser.add_argument("--bert_vocab", default=BASE_DIR + '/data/pretrain_model/bert_base_uncased', type=str)
        parser.add_argument("--task_name",default='mrpc',type=str,help="The name of the task to train.")
        # parser.add_argument("--output_dir",default=BASE_DIR + '/runnings/model/webq/rerank_1multanh2_listwise_with_answer_type_1score_webq_neg_' + str(N) + '_' + str(seed) + '_' + str(steps) + '/',type=str)
        parser.add_argument("--output_dir",default=BASE_DIR + '/runnings/model/webq/rerank_listwise_cat_negOrder_mlp3_with_answer_type_1score_webq_neg_' + str(N) + '_' + str(seed) + '_' + str(steps) + '/',type=str)
        parser.add_argument("--input_model_dir", default='0.9675389502344577_0.4803025192052977_3', type=str)
        # parser.add_argument("--T_file_name",default='T_bert_top' + str(N) + '_from5530.txt',type=str)
        parser.add_argument("--v_file_name",default='v_bert_top' + str(N) + '_from5530.txt',type=str)
        parser.add_argument("--t_file_name",default='t_bert_top' + str(N) + '_from5530.txt',type=str)
        parser.add_argument("--T_file_name",default='webq_T_bert_negOrder_1_n_top' + str(N) + '.txt',type=str)
        # parser.add_argument("--v_file_name",default='v_bert_top20_from5530.txt',type=str)
        # parser.add_argument("--t_file_name",default='t_bert_top20_from5530.txt',type=str)
        

        parser.add_argument("--T_model_data_name",default='train_all_518484_from_1_500000000.pkl',type=str)
        parser.add_argument("--v_model_data_name",default='dev_all_135428_from_v_bert_rel_answer_pairwise_1_500000000.pkl',type=str)
        parser.add_argument("--t_model_data_name",default='test_all_344985_from_1_500000000.pkl',type=str)
        ## Other parameters
        parser.add_argument("--group_size",default=N,type=int,help="")
        parser.add_argument("--cache_dir",default="",type=str,help="Where do you want to store the pre-trained models downloaded from s3")
        parser.add_argument("--max_seq_length",default=100,type=int)
        parser.add_argument("--do_train",default='true',help="Whether to run training.")
        parser.add_argument("--do_eval",default='true',help="Whether to run eval on the dev set.")
        parser.add_argument("--do_lower_case",default='True', action='store_true',help="Set this flag if you are using an uncased model.")
        parser.add_argument("--train_batch_size",default=1,type=int,help="Total batch size for training.")
        parser.add_argument("--eval_batch_size",default=100,type=int,help="Total batch size for eval.")
        parser.add_argument("--learning_rate",default=5e-5,type=float,help="The initial learning rate for Adam.")
        parser.add_argument("--num_train_epochs",default=5.0,type=float,help="Total number of training epochs to perform.")
        parser.add_argument("--warmup_proportion",default=0.1,type=float,)
        parser.add_argument("--no_cuda",action='store_true',help="Whether not to use CUDA when available")
        parser.add_argument("--local_rank",type=int,default=-1,help="local_rank for distributed training on gpus")
        parser.add_argument('--seed',type=int,default=seed,help="random seed for initialization")
        parser.add_argument('--gradient_accumulation_steps',type=int,default=steps,help="Number of updates steps to accumulate before performing a backward/update pass.")
        parser.add_argument('--server_ip', type=str, default='', help="Can be used for distant debugging.")
        parser.add_argument('--server_port', type=str, default='', help="Can be used for distant debugging.")  
        args = parser.parse_args()
        random.seed(args.seed)
        np.random.seed(args.seed)
        torch.manual_seed(args.seed)
        torch.cuda.manual_seed_all(args.seed)
        
        # if os.path.exists(args.output_dir) and os.listdir(args.output_dir) and args.do_train:
        #     raise ValueError("Output directory ({}) already exists and is not empty.".format(args.output_dir))
        if not os.path.exists(args.output_dir):
            os.makedirs(args.output_dir)
        fout_res = open(args.output_dir + 'result.log', 'w', encoding='utf-8')
        # import pdb; pdb.set_trace()
        # best_model_dir_name = main(fout_res, args)
        best_model_dir_name = '/home/chenwenliang/jiayonghui/gitlab/rerankinglab/runnings/model/webq/5563_rerank_listwise_cat_negOrder_mlp3_with_answer_type_1score_webq_neg_30_42_100/0.9170379474573668_0.650460179598878_2/'
        test(best_model_dir_name, fout_res, args)