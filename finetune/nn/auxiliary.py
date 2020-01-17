import math

import tensorflow as tf
from finetune.nn.nn_utils import dropout, norm
from finetune.util.shapes import shape_list
from finetune.util.positional_embeddings import add_timing_signal_from_position
from finetune.base_models.gpt.featurizer import conv1d

def layer_norm_with_custom_init(input_tensor, begin_norm_axis=-1, begin_params_axis=-1,
                                name=None, custom=False, pos_embed=None):
    """Run layer normalization on the last dimension of the tensor."""

    if custom:
        bert_dimension = shape_list(input_tensor)[1] - pos_embed

        bert_tensor = input_tensor[:,:bert_dimension]
        pos_tensor = input_tensor[:,bert_dimension:]

        bert_layer_norm = tf.contrib.layers.layer_norm(inputs=bert_tensor,
                                                       begin_norm_axis=begin_norm_axis,
                                                       begin_params_axis=begin_params_axis,
                                                       scope=name)
        pos_layer_norm = tf.contrib.layers.layer_norm(inputs=pos_tensor,
                                                      begin_norm_axis=begin_norm_axis,
                                                      begin_params_axis=begin_params_axis,
                                                      scope='pos_layer_norm')

        full_layer_norm = tf.concat([bert_layer_norm, pos_layer_norm], axis=1)

        return full_layer_norm
    else:
        return tf.contrib.layers.layer_norm(inputs=input_tensor, begin_norm_axis=-1, begin_params_axis=-1, scope=name)

def dense_with_custom_init(input_tensor,
                           output_dim,
                           activation,
                           kernel_initializer,
                           name='dense',
                           custom=False,
                           pos_embed=None,
                           proj_type='factorized'):
    """
    Arguments:
    - proj_type (str): defines the type of custom projection to perform.
                       'factorized' factorizes the initial text weights and randomly initialize auxiliary weights, like so:
                       [
                           [W, W, W, 0, 0],
                           [W, W, W, 0, 0],
                           [W, W, W, 0, 0],
                           [0, 0, 0, P, P],
                           [0, 0, 0, P, P],
                       ]
                       'downward' projects out the additional auxiliary information, like so:
                        [
                           [W, W, W],
                           [W, W, W],
                           [W, W, W],
                           [0, 0, 0],
                           [0, 0, 0],
                       ]
                       'downward_identity' passes along the text inputs only, like so:
                        [
                           [1, 0, 0],
                           [0, 1, 0],
                           [0, 0, 1],
                           [0, 0, 0],
                           [0, 0, 0],
                       ]
    """

    if custom:
        # text-relevant weights
        if proj_type == 'factorized' or proj_type == 'downward':
            if proj_type == 'factorized':
                weight_output_dim = output_dim-pos_embed
            else:
                weight_output_dim = output_dim
            # Subtracting pos_embed. input_tensor already includes context, and we
            # want separate weights for the words and the positional context
            original_weights = tf.get_variable(name+'/kernel',shape=(shape_list(input_tensor)[1]-pos_embed, weight_output_dim))
            original_bias = tf.get_variable(name+'/bias', shape=(weight_output_dim))
        elif proj_type == 'downward_identity':
            original_weights = tf.eye(shape_list(input_tensor)[1])
            original_bias = tf.zeros((batch, output_dim))
        
        # position-relevant weights
        if proj_type == 'factorized':
            position_weights = tf.get_variable(name+"/pos_weights",
                                            shape=(pos_embed, pos_embed))
            original_weights = tf.pad(original_weights, tf.constant([[0,pos_embed], [0,0]]))
            # Note: Need to keep the dimension of original_weights before we pad it. Below
            # we use output_dim, put if we want to have a non-square matrix for original_weights
            # we should saving the first dimension of original_weights
            position_weights = tf.pad(position_weights, tf.constant([[shape_list(input_tensor)[1]-pos_embed,0], [0,0]]))
            # This concat should blow up (or give the wrong dimensions)
            full_weights = tf.concat((original_weights, position_weights), axis=1)
 
            # Also using output_dim here in lieu of shape_list(original_weights)[0]
            # If we did that, it would be pos_embed + pos_embed + output_dim
            position_bias = tf.get_variable(name+"/pos_bias", shape=(pos_embed))
            full_bias = tf.concat((original_bias, position_bias), axis=0)

        elif proj_type == 'downward' or proj_type == 'downward_init':
            position_weights = tf.zeroes((pos_embed, weight_output_dim))
            full_weights = tf.concat((original_weights, position_weights), axis=0)
            full_bias = original_bias
        
        # dense operation
        z = tf.matmul(input_tensor, full_weights) + full_bias
        if activation is not None:
            return activation(z)
        else:
            return z

    else:
        return tf.layers.dense(input_tensor, output_dim, activation=activation, name=name, kernel_initializer=kernel_initializer)



def embed_context(context, featurizer_state, config, train):
    with tf.variable_scope("context_embedding"):
        context_dim = shape_list(context)[-1]
        context_weight = tf.get_variable(
            name="ce",
            shape=[context_dim, config.n_context_embed],
            initializer=tf.random_normal_initializer(stddev=config.context_embed_stddev),
        )
        context_bias = tf.get_variable(
            name="ca",
            shape=[config.n_context_embed],
            initializer=tf.zeros_initializer(),
        )
        c_embed = tf.add(tf.tensordot(context, context_weight, axes=[[-1], [0]]), context_bias)
    featurizer_state['context'] = c_embed


def embed_position(context, config, batch, seq):
    with tf.variable_scope("context_embedding"):
        context_dim = shape_list(context)[-1]
        context_channels = config.n_context_embed_per_channel * context_dim
        x = tf.zeros(shape=(batch, seq, context_channels))
        pos_embed = add_timing_signal_from_position(
            x,
            context,
            timescales = [
                [
                    (math.pi / 2) * (1/2500),
                    (25 * math.pi) * (1/2500)
                ]
            ] * context_dim
        ) / (float(context_channels) / config.context_embed_scale)
    return pos_embed


def add_context_embed(featurizer_state):
    if "context" in featurizer_state:
        context_embed = featurizer_state["context"]

        shape = shape_list(context_embed)
        if len(shape) == 4:
            # comparison / multiple choice
            flat_embed = tf.reshape(
                context_embed,
                [shape[0] * shape[1], shape[2], shape[3]],
            )
        else:
            flat_embed = context_embed

        seq_mask = tf.sequence_mask(featurizer_state['lengths'])
        for key in ['features', 'explain_out']:
            if key in featurizer_state:
                float_mask = tf.cast(seq_mask, tf.float32)
                binary_mask = tf.constant(1.) - float_mask
                flat_embed = flat_embed * tf.expand_dims(binary_mask, -1)
                sum_context = tf.reduce_sum(flat_embed, 1)
                mean_context = sum_context / tf.reduce_sum(float_mask)

                if len(shape) == 4:
                    mean_context = tf.reshape(
                        mean_context,
                        [shape[0], shape[1], shape[3]]
                    )

                featurizer_state[key] = tf.concat(
                    (featurizer_state[key], mean_context), -1
                )

        featurizer_state['sequence_features'] = tf.concat(
            (featurizer_state['sequence_features'], context_embed), -1
        )
