import os
import torch
import numpy as np
from torch.utils.data import Dataset, DataLoader
import torch.nn as nn
import pickle
from tqdm import tqdm
import argparse
from torch.nn import functional as F
from dataloader import DatasetMetaQA, DataLoaderMetaQA
from model import RelationExtractor
from torch.optim.lr_scheduler import ExponentialLR
import pandas as pd

def str2bool(v):
    if isinstance(v, bool):
       return v
    if v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        return True

parser = argparse.ArgumentParser()
parser.add_argument('--hops', type=str, default='1')
parser.add_argument('--ls', type=float, default=0.0)
parser.add_argument('--validate_every', type=int, default=5)
parser.add_argument('--model', type=str, default='Rotat3')
parser.add_argument('--kg_type', type=str, default='half')

parser.add_argument('--mode', type=str, default='eval')
parser.add_argument('--batch_size', type=int, default=1024)
parser.add_argument('--dropout', type=float, default=0.1)
parser.add_argument('--entdrop', type=float, default=0.0)
parser.add_argument('--reldrop', type=float, default=0.0)
parser.add_argument('--scoredrop', type=float, default=0.0)
parser.add_argument('--l3_reg', type=float, default=0.0)
parser.add_argument('--decay', type=float, default=1.0)
parser.add_argument('--shuffle_data', type=bool, default=True)
parser.add_argument('--num_workers', type=int, default=15)
parser.add_argument('--lr', type=float, default=0.0001)
parser.add_argument('--nb_epochs', type=int, default=90)
parser.add_argument('--gpu', type=int, default=0)
parser.add_argument('--neg_batch_size', type=int, default=128)
parser.add_argument('--hidden_dim', type=int, default=200)
parser.add_argument('--embedding_dim', type=int, default=256)
parser.add_argument('--relation_dim', type=int, default=30)
parser.add_argument('--use_cuda', type=bool, default=True)
parser.add_argument('--patience', type=int, default=5)
parser.add_argument('--freeze', type=str2bool, default=True)

os.environ["CUDA_VISIBLE_DEVICES"]="0,1,2,3,4,5,6,7"
args = parser.parse_args()


def prepare_embeddings(embedding_dict):
    entity2idx = {}
    idx2entity = {}
    i = 0
    embedding_matrix = []
    for key, entity in embedding_dict.items():
        entity2idx[key.strip()] = i
        idx2entity[i] = key.strip()
        i += 1
        embedding_matrix.append(entity)
    return entity2idx, idx2entity, embedding_matrix

def get_vocab(data):
    word_to_ix = {}
    maxLength = 0
    idx2word = {}
    for d in data:
            sent = d[1]
            for word in sent.split():
                if word not in word_to_ix:
                    idx2word[len(word_to_ix)] = word
                    word_to_ix[word] = len(word_to_ix)
                    
            length = len(sent.split())
            if length > maxLength:
                maxLength = length

    return word_to_ix, idx2word, maxLength

def preprocess_entities_relations(entity_dict, relation_dict, entities, relations):
    e = {}
    r = {}

    f = open(entity_dict, 'r')
    for line in f:
        line = line.strip().split('\t')
        ent_id = int(line[0])
        ent_name = line[1]
        e[ent_name] = entities[ent_id]
    f.close()

    f = open(relation_dict,'r')
    for line in f:
        line = line.strip().split('\t')
        rel_id = int(line[0])
        rel_name = line[1]
        r[rel_name] = relations[rel_id]
    f.close()
    return e,r

def inTopk(scores, ans, k):
    result = False
    topk = torch.topk(scores, k)[1]
    for x in topk:
        if x in ans:
            result = True
    return result

def validate(data_path, device, model, word2idx, entity2idx, model_name, return_hits_at_k):
    model.eval()
    data = preprocess_entities_relations(data_path)
    answers = []
    data_gen = data_generator(data=data, word2ix=word2idx, entity2idx=entity2idx)
    total_correct = 0
    error_count = 0

    hit_at_1 = 0
    hit_at_5 = 0
    hit_at_10 = 0

    candidates_with_scores = []
    writeCandidatesToFile=False
    
    for i in tqdm(range(len(data))):
        try:
            d = next(data_gen)
            head = d[0].to(device)
            question = d[1].to(device)
            ans = d[2]
            ques_len = d[3].unsqueeze(0)
            tail_test = torch.tensor(ans, dtype=torch.long).to(device)

            scores = model.get_score_ranked(head=head, sentence=question, sent_len=ques_len)[0]
            # candidates = qa_nbhood_list[i]
            # mask = torch.from_numpy(getMask(candidates, entity2idx)).to(device)
            # following 2 lines for no neighbourhood check
            mask = torch.zeros(len(entity2idx)).to(device)
            mask[head] = 1
            #reduce scores of all non-candidates
            new_scores = scores - (mask*99999)
            pred_ans = torch.argmax(new_scores).item()
            # new_scores = new_scores.cpu().detach().numpy()
            # scores_list.append(new_scores)
            if pred_ans == head.item():
                print('Head and answer same')
                print(torch.max(new_scores))
                print(torch.min(new_scores))
            # pred_ans = getBest(scores, candidates)
            # if ans[0] not in candidates:
            #     print('Answer not in candidates')
                # print(len(candidates))
                # exit(0)
            
            if writeCandidatesToFile:
                entry = {}
                entry['question'] = d[-1]
                head_text = idx2entity[head.item()]
                entry['head'] = head_text
                s, c =  torch.topk(new_scores, 200)
                s = s.cpu().detach().numpy()
                c = c.cpu().detach().numpy()
                cands = []
                for cand in c:
                    cands.append(idx2entity[cand])
                entry['scores'] = s
                entry['candidates'] = cands
                correct_ans = []
                for a in ans:
                    correct_ans.append(idx2entity[a])
                entry['answers'] = correct_ans
                candidates_with_scores.append(entry)


            if inTopk(new_scores, ans, 1):
                hit_at_1 += 1
            if inTopk(new_scores, ans, 5):
                hit_at_5 += 1
            if inTopk(new_scores, ans, 10):
                hit_at_10 += 1


            if type(ans) is int:
                ans = [ans]
            is_correct = 0
            if pred_ans in ans:
                total_correct += 1
                is_correct = 1
            else:
                num_incorrect += 1
            q_text = d[-1]
            answers.append(q_text + '\t' + str(pred_ans) + '\t' + str(is_correct))
        except:
            error_count += 1
        
    accuracy = total_correct/len(data)
    # print('Error mean rank: %f' % (incorrect_rank_sum/num_incorrect))
    # print('%d out of %d incorrect were not in top 50' % (not_in_top_50_count, num_incorrect))

    if return_hits_at_k:
        return answers, accuracy, (hit_at_1/len(data)), (hit_at_5/len(data)), (hit_at_10/len(data))
    else:
        return answers, accuracy

def writeToFile(lines, fname):
    f = open(fname, 'w')
    for line in lines:
        f.write(line + '\n')
    f.close()
    print('Wrote to ', fname)
    return

def set_bn_eval(m):
    classname = m.__class__.__name__
    if classname.find('BatchNorm1d') != -1:
        m.eval()

def get_chk_suffix():
    return '.chkpt'

def get_checkpoint_file_path(chkpt_path, model_name, num_hops, suffix, kg_type):
    return f"{chkpt_path}{model_name}_{num_hops}_{suffix}_{kg_type}"
        
def perform_experiment(data_path, mode, entity_path, relation_path, entity_dict, relation_dict, neg_batch_size, batch_size, shuffle, num_workers, nb_epochs, embedding_dim, hidden_dim, relation_dim, gpu, use_cuda,patience, freeze, validate_every, num_hops, lr, entdrop, reldrop, scoredrop, l3_reg, model_name, decay, ls, w_matrix, bn_list, kg_type, valid_data_path=None, test_data_path=None):
    entities = np.load(entity_path)
    relations = np.load(relation_path)
    e,r = preprocess_entities_relations(entity_dict, relation_dict, entities, relations)
    entity2idx, idx2entity, embedding_matrix = prepare_embeddings(e)
    data = preprocess_entities_relations(data_path, split=False)
    # data = pickle.load(open(data_path, 'rb'))
    word2ix,idx2word, max_len = get_vocab(data)
    hops = str(num_hops)
    device = torch.device(gpu if use_cuda else "cpu")

    dataset = DatasetMetaQA(data=data, word2ix=word2ix, relations=r, entities=e, entity2idx=entity2idx)

    model = RelationExtractor(embedding_dim=embedding_dim, hidden_dim=hidden_dim, vocab_size=len(word2ix), num_entities = len(idx2entity), relation_dim=relation_dim, pretrained_embeddings=embedding_matrix, freeze=freeze, device=device, entdrop = entdrop, reldrop = reldrop, scoredrop = scoredrop, l3_reg = l3_reg, model = model_name, ls = ls, w_matrix = w_matrix, bn_list=bn_list)

    checkpoint_path = '../../checkpoints/MetaQA/'
    if mode=='train':
        optimizer = torch.optim.Adam(model.parameters(), lr=lr)
        scheduler = ExponentialLR(optimizer, decay)
        optimizer.zero_grad()
        model.to(device)
        best_score = -float("inf")
        best_model = model.state_dict()
        no_update = 0
        data_loader = DataLoaderMetaQA(dataset, batch_size=batch_size, shuffle=True, num_workers=num_workers)
        for epoch in range(nb_epochs):
            phases = []
            for i in range(validate_every):
                phases.append('train')
            phases.append('valid')
            for phase in phases:
                if phase == 'train':
                    model.train()
                    if freeze == True:
                        # print('Freezing batch norm layers')
                        model.apply(set_bn_eval)
                    loader = tqdm(data_loader, total=len(data_loader), unit="batches")
                    running_loss = 0
                    for i_batch, a in enumerate(loader):
                        model.zero_grad()
                        question = a[0].to(device)
                        sent_len = a[1].to(device)
                        positive_head = a[2].to(device)
                        positive_tail = a[3].to(device)                    

                        loss = model(sentence=question, p_head=positive_head, p_tail=positive_tail, question_len=sent_len)
                        loss.backward()
                        optimizer.step()
                        running_loss += loss.item()
                        loader.set_postfix(Loss=running_loss/((i_batch+1)*batch_size), Epoch=epoch)
                        loader.set_description('{}/{}'.format(epoch, nb_epochs))
                        loader.update()
                    
                    scheduler.step()

                elif phase=='valid':
                    model.eval()
                    eps = 0.0001
                    answers, score = validate(model=model, data_path= valid_data_path, word2idx= word2ix, entity2idx= entity2idx, device=device, model_name=model_name, return_hits_at_k=False)
                    if score > best_score + eps:
                        best_score = score
                        no_update = 0
                        best_model = model.state_dict()
                        print(hops + " hop Validation accuracy increased from previous epoch", score)
                        _, test_score = validate(model=model, data_path= test_data_path, word2idx= word2ix, entity2idx= entity2idx, device=device, model_name=model_name, return_hits_at_k=False)
                        print('Test score for best valid so far:', test_score)
                        # writeToFile(answers, 'results_' + model_name + '_' + hops + '.txt')
                        suffix = ''
                        if freeze == True:
                            suffix = '_frozen'
                        checkpoint_file_name = get_checkpoint_file_path(checkpoint_path, model_name, num_hops, suffix, kg_type)+get_chk_suffix()
                        print('Saving checkpoint to ', checkpoint_file_name)
                        torch.save(model.state_dict(), checkpoint_file_name)
                    elif (score < best_score + eps) and (no_update < patience):
                        no_update +=1
                        print("Validation accuracy decreases to %f from %f, %d more epoch to check"%(score, best_score, patience-no_update))
                    elif no_update == patience:
                        print("Model has exceed patience. Saving best model and exiting")
                        torch.save(best_model, get_checkpoint_file_path(checkpoint_path, model_name, num_hops, '', kg_type)+ '_' + 'best_score_model' + get_chk_suffix() )
                        exit()
                    if epoch == nb_epochs-1:
                        print("Final Epoch has reached. Stopping and saving model.")
                        torch.save(best_model, get_checkpoint_file_path(checkpoint_path, model_name, num_hops, '', kg_type)+ '_' + 'best_score_model' + get_chk_suffix() )
                        exit()
    elif mode=='test':
        model_chkpt_file=get_checkpoint_file_path(checkpoint_path, model_name, num_hops, '', kg_type)+ '_' + 'best_score_model' + get_chk_suffix()
        
        print(model_chkpt_file)
        
        model.load_state_dict(torch.load(model_chkpt_file, map_location=lambda storage, loc: storage))
        model.to(device)
        # for parameter in model.parameters():
        #     parameter.requires_grad = False

        answers, accuracy, hits_at_1, hits_at_5, hits_at_10  = validate(model=model, data_path= test_data_path, word2idx= word2ix, entity2idx= entity2idx, device=device, model_name=model_name, return_hits_at_k=True)

        d = {
            'KG-Model': model_name,
            'KG-Type': kg_type,
            'hops': num_hops,
            'Accuracy': [accuracy], 
            'Hits@1': [hits_at_1],
            'Hits@5': [hits_at_5],
            'Hits@10': [hits_at_10]
            }
        df = pd.DataFrame(data=d)
        df.to_csv(f"final_results.csv", mode='a', index=False, header=False)       
                    
def preprocess_entities_relations(entity_dict, relation_dict, entities, relations):
    e = {}
    r = {}

    # Assuming `entity_dict` is the path to your entities dictionary file
    with open(entity_dict, 'r') as f:
        for line in f:
            line = line.strip().split('\t')  # or split by whatever delimiter is used
            # Assuming the format is "entity_name    entity_id"
            if len(line) == 2:
                entity_name, entity_id_str = line
                try:
                    ent_id = int(entity_id_str)
                    e[entity_name] = entities[ent_id]
                except ValueError:
                    # Handle cases where conversion to integer fails
                    print(f"Skipping line due to ValueError: {line}")

    # Do similar processing for relations
    with open(relation_dict, 'r') as f:
        for line in f:
            line = line.strip().split('\t')  # or split by whatever delimiter is used
            if len(line) == 2:
                relation_name, relation_id_str = line
                try:
                    rel_id = int(relation_id_str)
                    r[relation_name] = relations[rel_id]
                except ValueError:
                    # Handle cases where conversion to integer fails
                    print(f"Skipping line due to ValueError: {line}")

    return e, r



def data_generator(data, word2ix, entity2idx):
    for i in range(len(data)):
        data_sample = data[i]
        head = entity2idx[data_sample[0].strip()]
        question = data_sample[1].strip().split(' ')
        encoded_question = [word2ix[word.strip()] for word in question]
        if type(data_sample[2]) is str:
            ans = entity2idx[data_sample[2]]
        else:
            ans = [entity2idx[entity.strip()] for entity in list(data_sample[2])]

        yield torch.tensor(head, dtype=torch.long),torch.tensor(encoded_question, dtype=torch.long) , ans, torch.tensor(len(encoded_question), dtype=torch.long), data_sample[1]


# Parse command-line arguments
args = parser.parse_args()

model_name = args.model

# Define the base paths for the data and embeddings
data_base_path = 'MetaQA/'
embeddings_base_path = 'MetaQA/'
metaqa_base_path = 'MetaQA/'  # Add this line


# Use these base paths to construct full paths to the data and embedding files
hops_suffix = f"{args.hops}hop" if args.hops in ['1', '2', '3'] else args.hops
kg_suffix = '_half' if args.kg_type == 'half' else ''

# Now we can define kg_type because args has been parsed
kg_type = args.kg_type
print('KG type is', kg_type)

data_path = f'{data_base_path}qa_train_{hops_suffix}{kg_suffix}.txt'
valid_data_path = f'{data_base_path}qa_dev_{hops_suffix}.txt'
test_data_path = f'{data_base_path}qa_test_{hops_suffix}.txt'

# Embeddings and dictionaries are directly within the 'MetaQA' folder as per your screenshot
entity_embedding_path = 'E.npy'
relation_embedding_path = 'R.npy'
# Since entities.dict and relations.dict are in the MetaQA/raw folder
raw_data_path = f'{metaqa_base_path}raw/'

entity_dict = f'{raw_data_path}entities.dict'
relation_dict = f'{raw_data_path}relations.dict'



# Adjust w_matrix based on the model requirement
if model_name == 'TuckER':
    w_matrix_path = f'{embeddings_base_path}W.npy'
    w_matrix = w_matrix_path if os.path.exists(w_matrix_path) else None
else:
    w_matrix = None



#bn_list = []

#for i in range(3):
 #   bn = np.load(embedding_folder + '/bn' + str(i) + '.npy', allow_pickle=True)
  #  bn_list.append(bn.item())

perform_experiment(data_path=data_path, 
mode=args.mode,
entity_path=entity_embedding_path, 
relation_path=relation_embedding_path,
entity_dict=entity_dict, 
relation_dict=relation_dict, 
neg_batch_size=args.neg_batch_size, 
batch_size=args.batch_size,
shuffle=args.shuffle_data, 
num_workers=args.num_workers,
nb_epochs=args.nb_epochs, 
embedding_dim=args.embedding_dim, 
hidden_dim=args.hidden_dim, 
relation_dim=args.relation_dim, 
gpu=args.gpu, 
use_cuda=args.use_cuda, 
valid_data_path=valid_data_path,
test_data_path=test_data_path,
patience=args.patience,
validate_every=args.validate_every,
freeze=args.freeze,
num_hops=args.hops,
lr=args.lr,
entdrop=args.entdrop,
reldrop=args.reldrop,
scoredrop = args.scoredrop,
l3_reg = args.l3_reg,
model_name=args.model,
decay=args.decay,
ls=args.ls,
w_matrix=w_matrix,
bn_list=[],
kg_type=kg_type)
