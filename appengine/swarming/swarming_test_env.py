# Copyright 2013 The LUCI Authors. All rights reserved.
# Use of this source code is governed under the Apache License, Version 2.0
# that can be found in the LICENSE file.

import os
import sys

# swarming/
APP_DIR = os.path.dirname(os.path.realpath(os.path.abspath(__file__)))


def setup_test_env():
  """Sets up App Engine test environment."""
  # For application modules.
  sys.path.insert(0, APP_DIR)
  # TODO(maruel): Remove.
  sys.path.insert(0, os.path.join(APP_DIR, 'components', 'third_party'))

  from test_support import test_env
  test_env.setup_test_env()
