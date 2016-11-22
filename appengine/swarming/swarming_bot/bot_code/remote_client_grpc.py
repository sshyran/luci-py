# Copyright 2016 The LUCI Authors. All rights reserved.
# Use of this source code is governed under the Apache License, Version 2.0
# that can be found in the LICENSE file.

# This is a reimplementation of RemoteClientNative but it uses (will use)
# a gRPC method to communicate with a server instead of REST.

import json
import logging

import grpc
import google.protobuf.json_format
from proto import swarming_bot_pb2


class RemoteClientGrpc(object):
  """RemoteClientGrpc knows how to make calls via gRPC.
  """

  def __init__(self, server):
    logging.info('Communicating with host %s via gRPC', server)
    self._server = server
    self._channel = grpc.insecure_channel(server)
    self._stub = swarming_bot_pb2.BotServiceStub(self._channel)
    self._log_is_asleep = False

  def is_grpc(self):
    return True

  def initialize(self, quit_bit):
    pass

  @property
  def uses_auth(self):
    return False

  def get_authentication_headers(self):
    return {}

  def post_bot_event(self, _event_type, message, _attributes):
    logging.warning('Not yet implemented: posting bot event: %s', message)

  def post_task_update(self, task_id, bot_id, params,
                       stdout_and_chunk=None, _exit_code=None):
    logging.warning(
      'Not yet implemented: posting task update for task %s, bot %s: %s',
      task_id, bot_id, params)
    if stdout_and_chunk != None:
      logging.warning('stdout: %s', stdout_and_chunk[0])
    return True

  def post_task_error(self, _task_id, _bot_id, _message):
    logging.warning('Not yet implemented: posting task error')
    return True

  def _attributes_json_to_proto(self, json_attr, msg):
    msg.version = json_attr['version']
    for k, values in sorted(json_attr['dimensions'].iteritems()):
      pair = msg.dimensions.add()
      pair.name = k
      pair.values.extend(values)
    create_state_proto(json_attr['state'], msg.state)

  def do_handshake(self, attributes):
    request = swarming_bot_pb2.HandshakeRequest()
    self._attributes_json_to_proto(attributes, request.attributes)
    response = self._stub.Handshake(request)
    resp = {
        'server_version': response.server_version,
        'bot_version': response.bot_version,
        'bot_group_cfg_version': response.bot_group_cfg_version,
        'bot_group_cfg': {
            'dimensions': {
                d.name: d.values for d in response.bot_group_cfg.dimensions
            },
        },
    }
    logging.info('Completed handshake: %s', resp)
    return resp

  def poll(self, attributes):
    request = swarming_bot_pb2.PollRequest()
    self._attributes_json_to_proto(attributes, request.attributes)
    # TODO(aludwin): gRPC-specific exception handling
    response = self._stub.Poll(request)

    if response.cmd == swarming_bot_pb2.PollResponse.UPDATE:
      return 'update', response.version

    if response.cmd == swarming_bot_pb2.PollResponse.SLEEP:
      if not self._log_is_asleep:
        logging.info('Going to sleep')
        self._log_is_asleep = True
      return 'sleep', response.sleep_time

    if response.cmd == swarming_bot_pb2.PollResponse.TERMINATE:
      logging.info('Terminating!')
      return 'terminate', response.terminate_taskid

    if response.cmd == swarming_bot_pb2.PollResponse.RESTART:
      logging.info('Restarting: %s', response.restart_message)
      return 'restart', response.restart_message

    if response.cmd == swarming_bot_pb2.PollResponse.RUN:
      protoManifest = response.manifest
      manifest = {
        'bot_id': protoManifest.bot_id,
        'command': None, # only supports Isolated, but avoid key error
        'dimensions' : {
            key: val for key, val in protoManifest.dimensions.items()
        },
        'env': {
            key: val for key, val in protoManifest.env.items()
        },
        'grace_period': protoManifest.grace_period,
        'hard_timeout': protoManifest.hard_timeout,
        'io_timeout': protoManifest.io_timeout,
        'isolated': {
            'namespace': protoManifest.isolated.namespace,
            'input' : protoManifest.isolated.input,
            'server': self._server, #TODO(aludwin): make this work properly
        },
        'task_id': protoManifest.task_id,
      }
      logging.info('Received job manifest: %s', manifest)
      self._log_is_asleep = False
      return 'run', manifest

    raise ValueError('Unknown command in response: %s' % response)

  def get_bot_code(self, new_zip_fn, bot_version, _bot_id):
    # TODO(aludwin): exception handling, pass bot_id
    logging.info('Updating to version: %s', bot_version)
    request = swarming_bot_pb2.UpdateRequest()
    request.bot_version = bot_version
    response = self._stub.Update(request)
    with open(new_zip_fn, 'wb') as f:
      f.write(response.bot_code)

  def ping(self):
    pass


def create_state_proto(state_dict, message):
  """ Constructs a State message out of a state dict.

  Inspired by https://github.com/davyzhang/dict-to-protobuf, but all sub-dicts
  need to be encoded as google.protobuf.Structs because only Structs can handle
  free-form key-value pairs (and the mount points, for example, are not known
  at compile time).

  Why not use Struct for the *entire* message? It's because json_format.Parse
  expects the json to have a very specific format (all lists must be wrapped in
  a field called "values") that is too hard to enforce here. So we only use
  Structs where they're needed, and rely on the format of the State proto being
  correct for everything else.
  """
  for k, v in state_dict.iteritems():
    if isinstance(v, dict):
      sub_msg = getattr(message, k)
      json_val = json.dumps(v)
      google.protobuf.json_format.Parse(json_val, sub_msg)
    elif isinstance(v, list):
      l = getattr(message, k)
      l.extend(v)
    elif v != None:
      # setattr doesn't like setting "None" state_dict. Other falsy values are
      # ok. Also, setting something to its default value apparently has no
      # effect, so be ready to deal with it on the receiving side.
      #
      # Warning: setattr will throw if attr doesn't exist.
      # TODO(aludwin): catch in sane way
      setattr(message, k, v)
