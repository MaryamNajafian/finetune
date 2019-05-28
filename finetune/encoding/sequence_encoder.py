import warnings

import ipdb

from finetune.util.logging import truncate_text
from finetune.encoding.input_encoder import NLP
from finetune.errors import FinetuneError


def assign_associations(labels, associations, none_value):
    idx_lookups = [{} for _ in labels]
    for i, (doc_label, doc_association) in enumerate(zip(labels, associations)):
        active_label_idx = -1
        for label, association in zip(doc_label, doc_association):
            if label == none_value:
                continue
            active_label_idx += 1
            for bpe_idx, _, _, _ in association:
                idx_lookups[i][bpe_idx] = active_label_idx

    all_candidates = []

    for idx_lookup, doc_label, doc_association in zip(idx_lookups, labels, associations):
        candidates = {}
        if doc_label == none_value:
            continue

        for association in doc_association:
            for bpe_idx, candidate_idx, candidate_label, candidate_prob in association:
                if candidate_label == none_value or candidate_idx not in idx_lookup:
                    continue

                if idx_lookup[bpe_idx] not in candidates:
                    candidates[idx_lookup[bpe_idx]] = []

                candidates[idx_lookup[bpe_idx]].append((idx_lookup[candidate_idx], candidate_label, candidate_prob))

        # TODO some how sample these candidates eg maximum probabilities, to fit some schema
        candidates = {k: max(v, key=lambda x: x[2]) for k, v in candidates.items()} # for now just pick maximum prob
        all_candidates.append(candidates)
    return all_candidates


def finetune_to_indico_sequence(raw_texts, subseqs, labels, encoder=None, probs=None, none_value=None,
                                subtoken_predictions=False, associations=None):
    """
    Maps from the labeled substring format into the 'indico' format. This is the exact inverse operation to
    :meth indico_to_finetune_sequence:.

    The indico format is as follows:
        Raw text for X,
        Labels as a list of dicts, with each dict in the form:
        {
            'start': <Character index of the start of the labeled sequence>,
            'end': <Character index of the end of the labeled sequence>,
            'label': <A categorical label (int or string) that represents the category of the subsequence,
            'text': <Optionally, a field with the subsequence contained between the start and end.
        }

    The Labeled substring, or finetune internal, format is as follows.
    Each item of the data is a list strings of the form:
        ["The quick brown", "fox", "jumped over the lazy", ...]
    With the corresponding labels:
        ["PAD", "animal", "PAD", ...]

    It is the :param none_value: that is used to populate the PAD labels.
    :param data: A list of segmented text of the form list(list(str))
    :param labels: Categorical labels for each sub-string in data.
    :param none_value: The none value used to encode the input format.
    :return: Texts, annoatations both in the 'indico' format.
    """
    annotations = []
    if associations is not None:
        assoc_cleaned = assign_associations(labels, associations, none_value)
    else:
        assoc_cleaned = [None] * len(raw_texts)

    encoded_docs = encoder._encode(raw_texts)
    loop_vals = zip(raw_texts, subseqs, labels, probs or [None] * len(raw_texts), assoc_cleaned)
    for doc_idx, (raw_text, doc_seq, label_seq, prob_seq, associations_seq) in enumerate(loop_vals):
        tokens = encoded_docs.tokens[doc_idx]
        spacy_tokens = NLP(raw_text)
        spacy_token_starts = [token.idx for token in spacy_tokens]
        spacy_token_ends = [token.idx + len(token.text) for token in spacy_tokens]
        n_spacy_tokens = len(spacy_tokens)
        doc_annotations = []
        annotation_ranges = set()
        start_idx = 0
        end_idx = 0
        raw_annotation_start = 0
        for i, (sub_str, raw_label, confidences) in enumerate(zip(doc_seq, label_seq, prob_seq or [None] * len(doc_seq))):
            if not isinstance(raw_label, tuple):
                multi_label = False
                label_list = [raw_label]
            else:
                multi_label = True
                label_list = raw_label

            for label in label_list:
                stripped_text = sub_str.strip()

                raw_annotation_start = raw_text.find(stripped_text, raw_annotation_start)
                raw_annotation_end = raw_annotation_start + len(stripped_text)

                if raw_annotation_start == -1:
                    warnings.warn("Failed to find predicted sequence in text: {}.".format(
                        truncate_text(stripped_text)
                    ))
                    continue

                annotation_start = raw_annotation_start
                annotation_end = raw_annotation_end

                # if we don't want to allow subtoken predictions, adjust start and end to match
                # the start and ends of the nearest full tokens
                if not subtoken_predictions:
                    if multi_label:
                        start_idx = 0
                        end_idx = 0

                    if label != none_value:
                        # round to nearest token
                        while start_idx < n_spacy_tokens and annotation_start >= spacy_token_starts[start_idx]:
                            start_idx += 1
                        annotation_start = spacy_token_starts[start_idx - 1]
                        while end_idx < (n_spacy_tokens - 1) and annotation_end > spacy_token_ends[end_idx]:
                            end_idx += 1
                        annotation_end = spacy_token_ends[end_idx]

                text = raw_text[annotation_start:annotation_end]
                if label != none_value:
                    annotation = {
                        "start": int(annotation_start),
                        "end": int(annotation_end),
                        "label": label,
                        "text": text
                    }
                    if associations_seq is not None and len(doc_annotations) in associations_seq:
                        index, relationship, prob = associations_seq[len(doc_annotations)]
                        annotation["associations"] = {
                            "index": index,
                            "relationship": relationship,
                            "prob": prob
                        }
                    if confidences is not None:
                        annotation["confidence"] = confidences

                    # prevent duplicate annotation edge case
                    if (annotation_start, annotation_end, label) not in annotation_ranges:
                        annotation_ranges.add((annotation_start, annotation_end, label))
                        doc_annotations.append(annotation)

        doc_annotations = sorted([dict(items) for items in doc_annotations], key=lambda x: x['start'])
        annotations.append(doc_annotations)
    return raw_texts, annotations

        
def sort_by_start(annotations):
    return sorted(annotations, key=lambda annotation: annotation['start'])
    

def overlap(current_annotation, annotation):
    return (
        (current_annotation['start'] < annotation['end'] <= current_annotation['end']) or 
        (annotation['start'] < current_annotation['end'] <= annotation['end'])
    )


def overlap_handler(current_annotation, annotation, text):
    """
    Scenarios:
        <> --> current_annotation
        [] --> annotation
        
    1) < [ > ]
    2) [ < > ]
    3) < [ ] >
    """
    if current_annotation['start'] <= annotation['start']:
        first, second = current_annotation, annotation
    else:
        first, second = annotation, current_annotation
    
    final_delimiter = min(first['end'], second['end'])
    final_label = second['label'] if (second['end'] > first['end']) else first['label']
    end = max(first['end'], second['end'])

    first_chunk = {
        'start': first['start'],
        'end': second['start'],
        'label': first['label'],
        'text': text[first['start']:second['start']]
    }
    second_chunk = {
        'start': second['start'],
        'end': final_delimiter,
        'label': first['label'] | second['label'],
        'text': text[second['start']:final_delimiter]
    }
    third_chunk = {
        'start': final_delimiter,
        'end': end,
        'label': final_label,
        'text': text[final_delimiter:end]
    }
    chunks = [first_chunk, second_chunk, third_chunk]
    return chunks


def indico_to_finetune_sequence(texts, labels=None, encoder=None, multi_label=True, none_value=None,
                                subtoken_labels=False):
    """
    Maps from the 'indico' format sequence labeling data. Into a labeled substring format. This is the exact inverse of
    :meth finetune_to_indico_sequence:.

    The indico format is as follows:
        Raw text for X,
        Labels as a list of dicts, with each dict in the form:
         labeled sequence>,
            'end': <Character index of the end of the labeled sequence>,
            'label': <A categorical label (int or string) that represents the category of the subsequence,
            'text': <A field containing the sub-sequence contained between the start and end.
        }

    The Labeled substring, or finetune internal, format is as follows.
    Each item of the data is a list strings of the form:{
            'start': <Character index of the start of the
        ["The quick brown", "fox", "jumped over the lazy", ...]
    With the corresponding labels:
        ["PAD", "animal", "PAD", ...]

    It is the :param none_value: that is used to populate the PAD labels.

    :param texts: A list of raw text.
    :param labels: A list of targets of the form list(list(dict))).
    :param none_value: A categorical label to use as the none value.
    :return: Segmented Text, Labels of the form described above.
    """
    all_subseqs = []
    all_labels = []
    all_association_idx = []
    all_association_type = []
    all_idxs = []

    # placeholder for inference time
    if labels is None:
        labels = [[]] * len(texts)

    encoded_docs = encoder._encode(texts)

    for doc_idx, (text, label_seq) in enumerate(zip(texts, labels)):
        tokens = encoded_docs.tokens[doc_idx]
        token_ends = encoded_docs.char_locs[doc_idx]
        token_lengths = [encoder._token_length(token) for token in tokens]
        token_starts = [end - length for end, length in zip(token_ends, token_lengths)]
        n_tokens = len(tokens)

        label_seq = sorted(label_seq, key=lambda x: x["start"])
        last_loc = 0
        merged_annotations = []
        doc_labels = []

        doc_association_idx = []
        doc_association_type = []
        doc_current_label_idx = []

        # for each annotation
        queue = sorted(label_seq, key=lambda x: x['start'])
        for label in label_seq:
            label['label'] = set([label['label']])
        
        while len(queue):
            current_annotation = queue.pop(0)

            if current_annotation['start'] == current_annotation['end']:
                # degenerate annotation, continue
                continue
            
            # for each existing merged annotation
            for annotation in sort_by_start(merged_annotations):
                # check if overlap is possible
                if annotation['start'] > current_annotation['end']:
                    # no overlap possible, append and move on to next item in queue 
                    merged_annotations.append(current_annotation)
                    break
                # if the merged annotation overlaps, remove it and break it up
                # into it's component parts.  process each component individually
                elif overlap(current_annotation, annotation):
                    merged_annotations.remove(annotation)
                    split_annotations = overlap_handler(current_annotation, annotation, text)
                    queue = split_annotations + queue
                    break
            else:
                # annotations can only be added to the list of merged annotations once all 
                # of their conflicts have already been resolved
                merged_annotations.append(current_annotation)

        for annotation in merged_annotations:
            annotation['label'] = list(annotation['label'])

        merged_annotations = sort_by_start(merged_annotations)
        
        # Add none labels
        current_idx = 0
        all_annotations = []
        for annotation in merged_annotations:
            if annotation['start'] > current_idx:
                # Add none span
                all_annotations.append({
                    'start': current_idx,
                    'end': annotation['start'],
                    'text': text[current_idx:annotation['start']],
                    'label': [none_value]
                })
            # Copy over labeled span
            all_annotations.append(annotation)
            current_idx = annotation['end']

        # Add span for the rest of the document
        end_idx = max([annotation['end'] for annotation in all_annotations])
        if end_idx != len(text):
            all_annotations.append({
                'start': end_idx,
                'end': len(text),
                'text': text[end_idx:len(text)],
                'label': [none_value]
            })

        if not multi_label:
            # if `multi_label_sequences` is False, flatten labels
            for annotation in all_annotations:
                if len(annotation['label']) > 1:
                    raise FinetuneError(
                        "Found overlapping annotations: {}. \n"
                        "Please set `multi_label_sequences` to `True` in your config.".format(
                            annotation
                        )
                    )
                annotation['label'] = annotation[0]

        doc_subseqs = [annotation['text'] for annotation in all_annotations]
        doc_labels = [tuple(annotation['label']) for annotation in all_annotations]

        all_subseqs.append(doc_subseqs)
        all_labels.append(doc_labels)
        all_association_idx.append(doc_association_idx)
        all_association_type.append(doc_association_type)
        all_idxs.append(doc_current_label_idx)

    return all_subseqs, all_labels, all_association_type, all_association_idx, all_idxs
