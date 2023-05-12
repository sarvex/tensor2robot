# coding=utf-8
# Copyright 2021 The Tensor2Robot Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Lint as python3
"""Custom getter utilities to leverage existing models for MAML."""

from typing import List, Mapping, Optional, Text, Tuple

import gin
import six
from six.moves import zip
import tensorflow.compat.v1 as tf


@gin.configurable
class MAMLInnerLoopGradientDescent(object):
  """This custom getter allows to use existing graph models for MAML.

  MAML requires to alter the variables we train on during the inner loop
  optimization. Therefore, we need to keep track of variables and replace
  their values with the corresponding gradient steps in consecutive calls.
  This custom getter based implementation allows to use existing graph
  based models without modification simply by using variable scopes with our
  custom getter.

  A simple use case would be:
  We assume we have an inputs_list, a maml_model_fn, and an inner_loss_fn,
  all as defined in meta_t2r_models.

  We now can perform the inner maml loop simply by calling

  maml_inner_loop_instance = MAMLInnerLoopGradientDescent(learning_rate=0.1)
  outputs = maml_inner_loop_instance.inner_loop(inputs_list=inputs_list,
                                                maml_model_fn=maml_model_fn,
                                                inner_loss_fn=inner_loss_fn)

  """

  def __init__(self,
               learning_rate = 0.001,
               use_second_order = True,
               var_scope = None,
               learn_inner_lr = False):
    """Create an instance.

    Args:
      learning_rate: A scalar learning rate (tensor or float) for the inner
        loop gradient descent step(s). If learn_inner_lr, then the true learning
        rate is a learned variable initialized at `learning_rate`.
      use_second_order: If True, we will backpropagate through the gradients,
        using second order information. If False, we will stop the backprop
        computation to exclude the gradients, thus, only using first order
        information.
      var_scope: String specifying scope of variables to apply gradients to.
        If variable starts with this (e.g. "a_func/pose_fc0/weights" starts with
        "a_func/pose_fc" This can be used to implement MAML models that only
        adapt a subset of weights (e.g. only the fully connected layers in a
        large CNN). Note that the *outer* loop optimization will still train
        all the variables, unless told otherwise.
      learn_inner_lr: If True, use learned per-tf.Variable inner loop learning
        rates initialized at `learning_rate`.
    """
    self._variable_cache = []
    self._learning_rate = learning_rate
    self._use_second_order = use_second_order
    self._learn_inner_lr = learn_inner_lr
    self._custom_getter_variable_cache = {}
    self._var_scope = var_scope
    self._lr_cache = {}

  def _get_learning_rate(self, var_name):
    """Returns the learning rate variable for variable with name `var_name`."""
    try:
      return self._lr_cache[var_name]
    except KeyError:
      with tf.variable_scope(
          'inner_learning_rates', reuse=tf.AUTO_REUSE, use_resource=True):
        name = '_'.join(six.ensure_str(var_name).split('/')) + '_inner_lr'
        learning_rate = tf.get_variable(
            name, shape=(), dtype=tf.float32,
            initializer=tf.initializers.constant(self._learning_rate))
      self._lr_cache[var_name] = learning_rate
      return learning_rate

  def add_parameter_summaries(self):
    """Add parameter summaries for the MAML inner loop."""
    if self._learn_inner_lr:
      for name, var in self._lr_cache.items():
        tf.summary.scalar(f'inner_loop_learning_rates/{six.ensure_str(name)}', var)
    else:
      tf.summary.scalar('inner_loop_learning_rate',
                        tf.constant(self._learning_rate))

  def _create_variable_getter_fn(self):
    """Create a custom variable getter.

    Note, this function returns a callable which will update the internal state
    of the object, initially populates the self._custem_getter_variable_cache.

    Returns:
      A custom getter function.
    """

    def custom_getter_fn(getter, name, *args, **kwargs):
      """The original variable getter with a variable interception.

      This function essentially caches the variables generated by the original
      getter if the variables have not yet been initialized. Thereafter
      it will always return the cached variable which will be internally
      updated by the call self._compute_and_apply_gradients. Note, our
      custom getter will always reuse the variables within the defined scope.

      Args:
        getter: The `getter` passed to a `custom_getter`. Please see the
          documentation for `tf.get_variable`.
        name: The `name` argument passed to `tf.get_variable`.
        *args: See positional arguments passed to `tf.get_variable`.
        **kwargs: See keyword arguments passed to `tf.get_variable`.

      Returns:
        An instance of the variable using the original initializer.
      """
      if name not in self._custom_getter_variable_cache:
        self._custom_getter_variable_cache[name] = getter(name, *args, **kwargs)

      return self._custom_getter_variable_cache[name]

    return custom_getter_fn

  def _compute_and_apply_gradients(self, loss):
    """Compute the gradients for all variables and apply them to the variables.

    We alter the internal self._custom_getter_variable_cache with new
    "variables" for which a gradient descent step has been applied.

    Args:
      loss: The loss tensor we want to derive the gradients for.

    Raises:
      ValueError: In case we try to compute the gradients without ever having
        populated our custom_gette scope.
    """

    if not self._custom_getter_variable_cache:
      raise ValueError(
          'Our custom getter has to be invoked at least once before'
          'we can compute gradients.')

    # We keep track of the previously used variables.
    self._variable_cache.append(self._custom_getter_variable_cache)

    # The old cache contains the latest variable state.
    variable_cache_old = self._variable_cache[-1]
    # The new cache will contain the updated variables.
    self._custom_getter_variable_cache = {}

    variable_list = list(variable_cache_old.keys())
    gradients = tf.gradients(
        [loss], [variable_cache_old[name] for name in variable_list])
    for name, gradient in zip(variable_list, gradients):
      # In case we change the model in an iteration.
      ignore_var = (
          self._var_scope is not None and not name.startswith(self._var_scope))
      if (gradient is None or ignore_var):
        self._custom_getter_variable_cache[name] = variable_cache_old[name]
        continue
      if self._learn_inner_lr:
        learning_rate = self._get_learning_rate(name)
      else:
        learning_rate = self._learning_rate
      scaled_gradient = learning_rate * gradient
      if not self._use_second_order:
        scaled_gradient = tf.stop_gradient(scaled_gradient)
      self._custom_getter_variable_cache[name] = (
          variable_cache_old[name] - scaled_gradient)

  def _extract_train_loss(self, train_fn_result):
    """Extract the train loss from the train fn results.

    Args:
      train_fn_result: The output of model_train_fn, a function which creates a
        loss according to the abstract_model.AbstractT2RModel abstraction.
        Please see the AbstractT2rModel class for more documentation. Note,
        we assume that the labels are stored in the inputs_list.

    Returns:
      The training loss tensor.

    Raises:
      ValueError: If the output of model_train_fn is not a loss tensor or a
        tuple(loss, train_outputs).
    """
    if isinstance(train_fn_result, tf.Tensor):
      return train_fn_result
    elif isinstance(train_fn_result, tuple):
      return train_fn_result[0]
    raise ValueError('The model_train_fn should return a '
                     'tuple(loss, train_outputs) or loss.')

  def inner_loop(
      self,
      inputs_list,
      inference_network_fn,
      model_train_fn,
      mode=None,
      params=None
  ):
    """The inner loop MAML optimization.

    The inner_loop function iterates over the input feature list and creates
    a new custom_getter for every iteration. The first iteration will
    initialize the variables. In every additional invocation we will create a
    custom getter with the updated variables according to the latest gradient
    descent step.

    Args:
      inputs_list:  list/tuple of (feature, label) tuples. We will iterate over
        the inputs_list and take a gradient step after every step but the last,
        resulting in len(inputs_list) - 1 gradient updates.
      inference_network_fn: A function which creates an inference network
        acccording to abstract_model.AbstractT2RModel. Please see the
        AbstractT2RModel class for more documentation on the function
        definition. Note, we assume that the labels are stored in the
        inputs_list.
      model_train_fn: A function which creates a loss according to the
        abstract_model.AbstractT2RModel abstraction. Please see the
        AbstractT2RModel class for more documentation. Note, we assume that the
        labels are stored in the inputs_list.
      mode: (ModeKeys) Specifies if this is training, evaluation or prediction.
      params: An optional dict of hyper parameters that will be passed into
        input_fn and model_fn. Keys are names of parameters, values are basic
        python types. There are reserved keys for TPUEstimator,
        including 'batch_size'.

    Returns:
      outputs: A list of len 2 containing the unconditioned and final
        conditioned output tensors of the maml_model_fn. The conditioned output
        tensors will be optimized by MAML.
        optimized by MAML.
      inner_outputs: Additional outputs of the MAML inner step computation.
        In the case of e.g. reinforcement learning we might want
        to optimize the steps/updates using these internal steps.
      inner_losses: Additional losses of the MAML inner step computation.
    """
    val_features, val_labels = inputs_list[-1]

    inner_outputs = []
    inner_losses = []
    if params is None:
      params = {}
    params['is_inner_loop'] = True
    for train_features, train_labels in inputs_list[:-1]:
      with tf.variable_scope(
          'inner_loop', custom_getter=self._create_variable_getter_fn()):
        outputs = inference_network_fn(
            features=train_features,
            labels=train_labels,
            mode=mode,
            params=params)
        inner_outputs.append(outputs)
      train_fn_result = model_train_fn(
          features=train_features,
          labels=train_labels,
          inference_outputs=outputs,
          mode=mode,
          config=None,
          params=params)
      train_loss = self._extract_train_loss(train_fn_result)
      inner_losses.append(train_loss)
      # The following function call will change the internal state, it will
      # update self._custom_getter_variable_cache. The next query
      # to the getter will return the altered variable for which a gradient
      # descent step has been applied.
      self._compute_and_apply_gradients(train_loss)

    # Compute the final inner outputs and loss to monitor if the network
    # adaptation actually helps.
    final_train_features, final_train_labels = inputs_list[-2]
    with tf.variable_scope(
        'inner_loop', custom_getter=self._create_variable_getter_fn()):
      final_inner_outputs = inference_network_fn(
          features=final_train_features,
          labels=final_train_labels,
          mode=mode,
          params=params)
      inner_outputs.append(final_inner_outputs)
    final_train_fn_result = model_train_fn(
        features=final_train_features,
        labels=final_train_labels,
        inference_outputs=final_inner_outputs,
        mode=mode,
        config=None,
        params=params)
    inner_losses.append(self._extract_train_loss(final_train_fn_result))

    with tf.variable_scope(
        'inner_loop', custom_getter=self._create_variable_getter_fn()):
      # Compute the conditioned outputs, the actual outputs of the overall
      # model. These outputs are used in the outer loop to determine the overall
      # loss.
      params['is_inner_loop'] = False
      conditioned_outputs = inference_network_fn(
          features=val_features, labels=val_labels, mode=mode, params=params)

    # Compute unconditioned outputs. These outputs are helpful to gain insights
    # into the model changes due to the inner loop. Typically, these outputs
    # are only used for summary generations, therefore do not add a big
    # overhead.
    with tf.variable_scope('inner_loop', reuse=True):
      params['is_inner_loop'] = True
      unconditioned_outputs = inference_network_fn(
          features=val_features, labels=val_labels, mode=mode, params=params)

    return [unconditioned_outputs,
            conditioned_outputs], inner_outputs, inner_losses
