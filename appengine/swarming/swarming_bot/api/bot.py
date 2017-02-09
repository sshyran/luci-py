# Copyright 2014 The LUCI Authors. All rights reserved.
# Use of this source code is governed under the Apache License, Version 2.0
# that can be found in the LICENSE file.

"""Bot interface used in bot_config.py."""

import inspect
import logging
import os
import sys
import time

import os_utilities

# Method could be a function - pylint: disable=R0201


def _get_stripper(paths):
  """Returns a function to strip common path prefixes.

  There are 3 kinds of paths:
    - relative paths
    - absolute paths
    - absolute paths in stdlib
  """
  if not paths:
    return lambda f: f

  stdlib = os.path.dirname(os.__file__)
  # Find the common root for paths not in stdlib and not relative.
  split_paths = [
    [c for c in p.split(os.path.sep) if c] for p in paths
    if os.path.isabs(p) and not p.startswith(stdlib)
  ]
  common = None
  if split_paths:
    common = []
    for c1, c2 in zip(min(split_paths), max(split_paths)):
      if c1 != c2:
        break
      common.append(c1)
    if common:
      if sys.platform == 'win32':
        common = os.path.sep.join(common)
      else:
        common = os.path.sep + os.path.sep.join(common)

  def stripper(f):
    if f.startswith(stdlib):
      return f[len(stdlib)+1:]
    if os.path.isabs(f) and common:
      return f[len(common)+1:]
    if f.startswith('./'):
      return f[2:]
    return f
  return stripper


def _make_stack():
  """Returns a well formatted call stack."""
  frame = inspect.currentframe().f_back
  frames = []
  while frame and len(frames) < 50:
    frames.append(frame)
    frame = frame.f_back
  strip = _get_stripper(f.f_code.co_filename for f in frames)
  return '\n'.join(
      '  %-2d %s:%s:%s()' % (
        i, strip(f.f_code.co_filename), f.f_lineno, f.f_code.co_name)
      for i, f in enumerate(frames))


class Bot(object):
  def __init__(
      self, remote, attributes, server, server_version, base_dir,
      shutdown_hook):
    # Do not expose attributes for now, as attributes may be refactored.
    assert server is None or not server.endswith('/'), server
    self._attributes = attributes or {}
    self._base_dir = base_dir
    self._bot_group_cfg_ver = None
    self._remote = remote
    self._server = server
    self._server_side_dimensions = {}
    self._server_version = server_version
    self._shutdown_hook = shutdown_hook

  @property
  def base_dir(self):
    """Returns the working directory.

    It is normally the current working directory, e.g. os.getcwd() but it is
    preferable to not assume that.
    """
    return self._base_dir

  @property
  def dimensions(self):
    """The bot's current dimensions.

    Dimensions are relatively static and not expected to change much. They
    should change only when it effectively affects the bot's capacity to execute
    tasks.

    Includes both bot supplied dimensions (as returned by get_dimensions
    bot_config.py hook) and server defined ones (as obtained during handshake
    with the server).

    Server defined dimensions are specified in bots.cfg configuration file on
    the server side. They completely override corresponding bot supplied
    dimensions.

    For example, if bot_config.get_dimensions() returns "pool:Foo"
    and bots.cfg defines "pool:Bar", then the bot will have "pool:Bar"
    dimension. It will NOT be a joined "pool:[Foo,Bar]" dimension.

    That way server can be sure that 'pool' dimension used for the bot is what
    it should be, even if the bot is misbehaving or maliciously trying to move
    itself to a different pool. By forcefully overriding dimensions on the
    server side we can use them as security boundaries.
    """
    return self._attributes.get('dimensions', {}).copy()

  @property
  def id(self):
    """Returns the bot's ID."""
    return self.dimensions.get('id', ['unknown'])[0]

  @property
  def remote(self):
    """RemoteClient to talk to the server.

    Should not be normally used by bot_config.py for now.
    """
    return self._remote

  @property
  def server(self):
    """URL of the swarming server this bot is connected to.

    It includes the https:// prefix but without trailing /, so it looks like
    "https://foo-bar.appspot.com".
    """
    return self._server

  @property
  def server_version(self):
    """Version of the server's implementation.

    The form is nnn-hhhhhhh for pristine version and nnn-hhhhhhh-tainted-uuuu
    for non-upstreamed code base:
      nnn: revision pseudo number
      hhhhhhh: git commit hash
      uuuu: username
    """
    return self._server_version

  @property
  def state(self):
    """Current bot state dict, as sent to the server.

    It is accessible from the UI and usually contains various helpful info about
    the bot status.

    The state may change often, but it can't be used in scheduling decisions.
    """
    return self._attributes.get('state', {}).copy()

  @property
  def swarming_bot_zip(self):
    """Absolute path to the swarming_bot.zip file.

    The bot itself is run as swarming_bot.1.zip or swarming_bot.2.zip. Always
    return swarming_bot.zip since this is the script that must be used when
    starting up.

    This is generally used by bot_config.setup_bot() when setting up the bot to
    automatically start upon boot.
    """
    return os.path.join(self.base_dir, 'swarming_bot.zip')

  def post_event(self, event_type, message):
    """Posts an event to the server."""
    self._remote.post_bot_event(event_type, message, self._attributes)

  def post_error(self, message):
    """Posts given string as a failure.

    This is used in case of internal code error. It traps exception.

    Include a full stack trace, because sometimes the error is not sufficient
    by itself.
    """
    logging.error('Error: %s\n%s', self._attributes, message)
    stack = '\nCalling stack:\n%s' % _make_stack()
    try:
      self.post_event('bot_error', '%s%s' % (message.rstrip(), stack))
    except Exception:
      logging.exception('post_error(%s) failed.%s', message, stack)

  def restart(self, message):
    """Reboots the machine.

    If the reboot is successful, never returns: the process should just be
    killed by OS.

    If reboot fails, logs the error to the server and moves the bot to
    quarantined mode.
    """
    self.post_event('bot_rebooting', message)
    if self._shutdown_hook:
      try:
        self._shutdown_hook(self)
      except Exception as e:
        logging.exception('shutdown hook failed: %s', e)
    # os_utilities.restart should never return, unless restart is not happening.
    # If restart is taking longer than N minutes, it probably not going to
    # finish at all. Report this to the server.
    try:
      os_utilities.restart(message, timeout=15*60)
    except LookupError:
      # This is a special case where OSX is deeply hosed. In that case the disk
      # is likely in read-only mode and there isn't much that can be done. This
      # exception is deep inside pickle.py. So notify the server then hang in
      # there.
      self.post_error('This host partition is bad; please fix the host')
      while True:
        time.sleep(1)
    self.post_error('Bot is stuck restarting for: %s' % message)

  def _update_bot_group_cfg(self, cfg_version, cfg):
    """Called internally to update server-provided per-bot config.

    This is called once, right after the handshake and it may modify values of
    'state' and 'dimensions' (by augmenting them with server-provided details).

    It is done only to make this information available to bot_config.py hooks.
    The server would still enforce the dimensions with each '/poll' call.

    See docs for '/handshake' call for the format of 'cfg' dict.
    """
    self._bot_group_cfg_ver = cfg_version
    self._server_side_dimensions = (cfg or {}).get('dimensions')
    # Apply changes to 'self._attributes'.
    self._update_dimensions(self._attributes.get('dimensions') or {})
    self._update_state(self._attributes.get('state') or {})

  def _update_dimensions(self, new_dimensions):
    """Called internally to update Bot.dimensions."""
    dimensions = new_dimensions.copy()
    dimensions.update(self._server_side_dimensions)
    self._attributes['dimensions'] = dimensions

  def _update_state(self, new_state):
    """Called internally to update Bot.state."""
    state = new_state.copy()
    state['bot_group_cfg_version'] = self._bot_group_cfg_ver
    self._attributes['state'] = state
