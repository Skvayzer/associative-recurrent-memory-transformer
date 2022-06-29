import torch
import torch.nn.functional as F
from typing import List, Optional, Tuple, Union
from transformers import PreTrainedModel, AutoModel
# from modeling_t5 import T5ForConditionalGeneration

import math

SEGMENTABLE = ['input_ids', 'inputs_embeds', 'token_type_ids', 'position_ids', 'attention_mask']
PAD_ZEROS = ['token_type_ids', 'attention_mask']


class RMTEncoderDecoderForConditionalGeneration():
    def __init__(self, config=None, base_model=None, **kwargs):
        if config is not None:
            self.config = config
            self.model = AutoModel(config, **kwargs)
        
        if base_model is not None:
            self.model = base_model


    def from_pretrained(from_pretrained, **kwargs):
        # print(f'Creating from pretrained: {from_pretrained}')
        base_model = AutoModel.from_pretrained(from_pretrained, **kwargs)
        rmt = RMTEncoderDecoderForConditionalGeneration(base_model=base_model)
        rmt.from_pretrained = from_pretrained
        return rmt
        

    def set_params(self, tokenizer,
                    encode_plus_kwargs={},
                    model_attr='', 
                    drop_empty_segments=True,
                    backbone_cls=None,
                    input_size=None, 
                    input_seg_size=None, 
                    num_mem_tokens=0, 
                    bptt_depth=-1):
        # print('model attr: ', model_attr)

        if backbone_cls is not None:
            self.model = backbone_cls.from_pretrained(self.from_pretrained)

        if model_attr:
            self.encoder = getattr(self.model, model_attr).encoder
        else:
            self.encoder = self.model.encoder
        self.embeddings = self.encoder.embed_tokens
        self.input_size = self.embeddings.weight.shape[0] if input_size is None else input_size
        self.input_seg_size = input_seg_size

        self.bptt_depth = bptt_depth
        self.pad_token_id = tokenizer.pad_token_id
        self.eos_token = torch.tensor([tokenizer.eos_token_id])
        self.tokenizer = tokenizer
        self.encode_plus_kwargs = encode_plus_kwargs
        # self.dot_id = tokenizer.encode('.')[1]
        # print(pad_token_id, eos_token_id)
        self.num_mem_tokens = num_mem_tokens
        self.drop_empty_segments = drop_empty_segments
        self.extend_word_embeddings()
        

    def set_memory(self, memory=None):
        if memory is None:
            mem_token_ids = self.mem_token_ids.to(device=self.device)
            # print('setting memory')
            # print('mem_token_ids', mem_token_ids.shape)
            memory = self.embeddings(mem_token_ids)
            # print('memory', memory.shape)
        return memory
    
    def extend_word_embeddings(self):
        vocab_size = self.embeddings.weight.shape[0]
        extended_vocab_size = vocab_size + self.num_mem_tokens
        self.mem_token_ids = torch.arange(vocab_size, vocab_size + self.num_mem_tokens)
        self.resize_token_embeddings(extended_vocab_size)
        self.embeddings = self.encoder.embed_tokens


    def __call__(self, input_ids, **kwargs):
        memory = self.set_memory()
        segmented = self.pad_and_segment(input_ids)
        segmented = list(zip(*segmented))
        segmented = segmented + [segmented[0]]
        for seg_num, segment_data in enumerate(segmented):
            input_ids, attention_mask, token_type_ids = segment_data
            if memory.ndim == 2:
                memory = memory.repeat(input_ids.shape[0], 1, 1)
            if (self.bptt_depth > -1) and (len(segmented) - seg_num > self.bptt_depth): 
                memory = memory.detach()

            seg_kwargs = dict(**kwargs)
            if self.drop_empty_segments:
                non_empty_mask = [not torch.equal(input_ids[i], self.empty) for i in range(len(input_ids))]
                if sum(non_empty_mask) == 0:
                    continue
                input_ids = input_ids[non_empty_mask]
                attention_mask = attention_mask[non_empty_mask]
                token_type_ids = token_type_ids[non_empty_mask]
                seg_kwargs['labels'] = seg_kwargs['labels'][non_empty_mask]

                inputs_embeds = self.embeddings(input_ids)
                inputs_embeds[:, 1:1+self.num_mem_tokens] = memory[non_empty_mask]

            else:
                inputs_embeds = self.embeddings(input_ids)
                inputs_embeds[:, 1:1+self.num_mem_tokens] = memory

            seg_kwargs['inputs_embeds'] = inputs_embeds
            seg_kwargs['attention_mask'] = attention_mask

            out = self.model.forward(**seg_kwargs, output_hidden_states=True)
            if self.drop_empty_segments:
                memory[non_empty_mask] = out.encoder_hidden_states[-1][:, :self.num_mem_tokens]
            else:
                memory = out.encoder_hidden_states[-1][:, :self.num_mem_tokens]
            # print('out', out.keys())
            # print('memory3',  memory.shape)

        # print('out,', out.keys())

        return out


    def generate(self, input_ids, **kwargs):
        memory = self.set_memory()
        segmented = self.pad_and_segment(input_ids)
        min_length, max_length = None, None
        if 'min_length' in kwargs:
            min_length = kwargs.pop('min_length')
        if 'max_length' in kwargs:
            max_length = kwargs.pop('max_length')
        segmented = list(zip(*segmented))
        for seg_num, segment_data in enumerate(segmented):
            input_ids, attention_mask, token_type_ids = segment_data
            if memory.ndim == 2:
                memory = memory.repeat(input_ids.shape[0], 1, 1)
            if (self.bptt_depth > -1) and (len(segmented) - seg_num > self.bptt_depth): 
                memory = memory.detach()

            seg_kwargs = dict(**kwargs)
            if self.drop_empty_segments:
                non_empty_mask = [not torch.equal(input_ids[i], self.empty) for i in range(len(input_ids))]
                if sum(non_empty_mask) == 0:
                    continue
                input_ids = input_ids[non_empty_mask]
                attention_mask = attention_mask[non_empty_mask]
                token_type_ids = token_type_ids[non_empty_mask]

                inputs_embeds = self.embeddings(input_ids)
                inputs_embeds[:, 1:1+self.num_mem_tokens] = memory[non_empty_mask]

            else:
                inputs_embeds = self.embeddings(input_ids)
                inputs_embeds[:, 1:1+self.num_mem_tokens] = memory
                
            seg_kwargs['inputs_embeds'] = inputs_embeds
            seg_kwargs['attention_mask'] = attention_mask
            # print('seg_num', 'len(segmented)')
            # print(seg_num, len(segmented))
            if seg_num < len(segmented)-1:
                labels = torch.zeros(inputs_embeds.shape[0], inputs_embeds.shape[1], device=inputs_embeds.device, dtype=input_ids.dtype)
                out = self.model.forward(**seg_kwargs, output_hidden_states=True, labels=labels)
                if self.drop_empty_segments:
                    memory[non_empty_mask] = out.encoder_hidden_states[-1][:, :self.num_mem_tokens]
                else:
                    memory = out.encoder_hidden_states[-1][:, :self.num_mem_tokens]
            else:
                # print('\n\n\nGENERATION')
                out = self.model.generate(**seg_kwargs, output_hidden_states=True, min_length=min_length, max_length=max_length)

        # print('\n\n\n\nout,', out.keys())
            
        return out

    def pad_and_segment(self, input_ids):
        
        sequence_len = input_ids.shape[1]
        input_seg_size = self.input_size - self.num_mem_tokens - 1
        if self.input_seg_size is not None and self.input_seg_size < input_seg_size:
            input_seg_size = self.input_seg_size
            
        n_segments = math.ceil(sequence_len / input_seg_size)

        augmented_inputs = []
        for input in input_ids:
            # print('input != self.pad_token_id ', (input != self.pad_token_id).shape, (input != self.pad_token_id).sum())
            # 1/0
            input = input[input != self.pad_token_id][:-1]

            # Duplicate question
            decoded_input = self.tokenizer.decode(input)
            # print("\n\n\ninput was: ", decoded_input)
            decoded_input = self.duplicate_question(decoded_input)
            # print("input became: ", decoded_input)
            input = self.tokenizer.encode_plus(decoded_input, return_tensors='pt')['input_ids'][0]

            seg_sep_inds = [0] + list(range(len(input), 0, -input_seg_size))[::-1] # chunk so that first segment has various size
            input_segments = [input[s:e] for s, e in zip(seg_sep_inds, seg_sep_inds[1:])]
            # print('input_segments', input_segments)
            # print('input_segments', [len(i) for i in input_segments])

            def pad_add_special_tokens(tensor, seg_size):
                tensor = torch.cat([
                                    # self.cls_token.to(device=self.device),
                                    self.mem_token_ids.to(device=self.device),
                                    # self.sep_token.to(device=self.device),
                                    tensor.to(device=self.device),
                                    # self.sep_token.to(device=self.device),
                                    self.eos_token.to(device=self.device)
                                    ])
                pad_size = seg_size - tensor.shape[0]
                if pad_size > 0:
                    tensor = F.pad(tensor, (0, pad_size))
                return tensor

            input_segments = [pad_add_special_tokens(t, self.input_size) for t in input_segments]
            empty = torch.Tensor([]).int()
            self.empty = pad_add_special_tokens(empty, self.input_size)
            empty_segments = [self.empty for i in range(n_segments - len(input_segments))]
            input_segments = empty_segments + input_segments

            augmented_input = torch.cat(input_segments)
            augmented_inputs.append(augmented_input)
            
        augmented_inputs = torch.stack(augmented_inputs)
        attention_mask = torch.ones_like(augmented_inputs)
        attention_mask[augmented_inputs == self.pad_token_id] = 0

        token_type_ids = torch.zeros_like(attention_mask)

        input_segments = torch.chunk(augmented_inputs, n_segments, dim=1)
        attention_mask = torch.chunk(attention_mask, n_segments, dim=1)
        token_type_ids = torch.chunk(token_type_ids, n_segments, dim=1)
    
        return input_segments, attention_mask, token_type_ids


    def duplicate_question(self, text, symbol_gap=100):
        # print('\n\n\nsource text len ', len(text))
        D_pos = text.find('(D)')
        dot_pos = text[D_pos:].find('.')
        question = text[:D_pos + dot_pos]

        shortened_text = text[:-(D_pos + dot_pos + symbol_gap)]
        # print('\n\n\nshortened text len ', len(shortened_text)) 
        last_dot_pos = len(shortened_text) - shortened_text[::-1].find('.')
        shortened_text = shortened_text[:last_dot_pos]
        # print('\n\n\nshortened text len ', len(shortened_text)) 
        text = shortened_text + question
        # print('\n\n\ndoubled text len ', len(text)) 

        return text


    def to(self, device):
        self.model = self.model.to(device)
        
    
    def cuda(self):
        self.model.cuda()


    def __getattr__(self, attribute):
        return getattr(self.model, attribute)


    def parameters(self, **kwargs):
        return self.model.parameters(**kwargs)

    def named_parameters(self, **kwargs):
        return self.model.named_parameters(**kwargs)