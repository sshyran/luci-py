# Copyright 2016 The LUCI Authors. All rights reserved.
# Use of this source code is governed under the Apache License, Version 2.0
# that can be found in the LICENSE file.

import collections
import endpoints
from six.moves import http_client
import json
import logging
import os
import posixpath

from endpoints import protojson
from protorpc import message_types
from protorpc import messages
from protorpc import remote
import flask
import webapp2
import flask

from components import template

import discovery
import partial

PROTOCOL = protojson.EndpointsProtoJson()

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
CORS_HEADERS = {
    'Access-Control-Allow-Origin':
    '*',
    'Access-Control-Allow-Headers':
    ('Origin, Authorization, Content-Type, Accept, User-Agent'),
    'Access-Control-Allow-Methods': ('DELETE, GET, OPTIONS, POST, PUT')
}


def decode_field(field, value):
  """Like PROTOCOL.decode_field, but also supports booleans."""
  if isinstance(field, messages.BooleanField):
    value = value.lower()
    if value == 'true':
      return True
    elif value == 'false':
      return False
    else:
      raise ValueError('boolean field must be either "true" or "false"')
  return PROTOCOL.decode_field(field, value)


def decode_message(remote_method_info, request):
  """Decodes a protorpc message from a Flask request.

  If method accepts a resource container, parses field values from URL too.
  """
  req_msg = endpoints.ResourceContainer.get_request_message(remote_method_info)
  if isinstance(req_msg, endpoints.ResourceContainer):
    res_container = req_msg
    body_type = req_msg.body_message_class
  else:
    res_container = None
    body_type = remote_method_info.request_type

  body = PROTOCOL.decode_message(body_type, request.body)
  if res_container:
    result = res_container.combined_message_class()
    for f in body.all_fields():
      setattr(result, f.name, getattr(body, f.name))
  else:
    result = body

  # Read field values from query string parameters or URL path.
  if res_container or request.method == 'GET':
    if request.method == 'GET':
      # In addition to standard ResourceContainer request type, we also support
      # GET request handlers that use Message instead of ResourceContainer,
      # because it is non-ambiguous (because GET requests cannot have body).
      param_fields = result.all_fields()
    else:
      param_fields = res_container.parameters_message_class.all_fields()
    for f in param_fields:
      if f.name in request.args:
        values = [request.args[f.name]]
      else:
        values = request.values.get_list(f.name)
      if values:
        values = [decode_field(f, v) for v in values]
        if f.repeated:
          getattr(result, f.name).extend(values)
        else:
          setattr(result, f.name, values[0])
  return result


def cors_handler():
  return flask.Response(headers=CORS_HEADERS)


def path_handler_factory(api_class, api_method, service_path):
  """Returns a Flask handler function for the API methods."""

  def path_handler():
    headers = CORS_HEADERS

    api = api_class()
    api.initialize_request_state(
        remote.HttpRequestState(
            remote_host=None,
            remote_address=flask.request.values['remote_addr'],
            server_host=flask.request.values['host'],
            server_port=flask.request.values['server_port'],
            http_method=flask.request.values['method'],
            service_path=service_path,
            headers=flask.request.headers.items()))

    try:
      req = decode_message(api_method.remote, flask.request)
      # Check that required fields are populated.
      req.check_initialized()
    except (messages.DecodeError, messages.ValidationError, ValueError) as ex:
      response = {'error': {'message': ex.message}}
      return (response, http_client.BAD_REQUEST, headers)
    try:
      res = api_method(api, req)
    except endpoints.ServiceException as ex:
      response = {'error': {'message': ex.message}}
      return (response, ex.http_status, headers)
    if isinstance(res, message_types.VoidMessage):
      return (None, http_client.NO_CONTENT, headers)
    # Flask jsonifies Python dicts, so this format is more convenient.
    response = json.loads(PROTOCOL.encode_message(res))
    if flask.request.get('fields'):
      try:
        # PROTOCOL.encode_message checks that the message is initialized
        # before dumping it directly to JSON string. Therefore we can't
        # mask the protocol buffer (if masking removes a required field
        # then encode_message will fail). Instead, call encode_message
        # first, mask the dict,and dump it back to JSON.
        response = partial.mask(response, flask.request.get('fields'))
      except (partial.ParsingError, ValueError) as e:
        # Log the error but return the full response.
        logging.warning('Ignoring erroneous field mask %r: %s',
                        flask.request.get('fields'), e)
    return (response, http_client.OK, headers)

  return path_handler


def api_routes(api_classes, base_path='/_ah/api', regex='[^/]+'):
  """Creates routes for the given Endpoints v1 services.

  Args:
    api_classes: A list of protorpc.remote.Service classes to create routes for.
    base_path: The base path under which all service paths should exist. If
      unspecified, defaults to /_ah/api.
    regex: Regular expression to allow in path parameters.

  Returns:
    A list of tuples, each consisting of three parts: a URL rule string,
    a function that handles the given endpoint, and an optional list of strings
    of acceptable HTTP methods.
  """

  routes = []

  # Add routes for each class.
  for api_class in api_classes:
    api_base_path = '%s/%s/%s' % (base_path, api_class.api_info.name,
                                  api_class.api_info.version)
    templates = set()

    # Add routes for each method of each class.
    for _, method in sorted(api_class.all_remote_methods().items()):
      info = method.method_info
      method_path = info.get_path(api_class.api_info)
      method_path = method_path.replace('{', '<').replace('}', ':%s>' % regex)
      t = posixpath.join(api_base_path, method_path)
      http_method = info.http_method.upper() or 'POST'
      handler = path_handler_factory(api_class, method, api_base_path)
      routes.append((t, handler, [http_method]))
      templates.add(t)

    # Add routes for HTTP OPTIONS (to add CORS headers) for each method.
    for t in sorted(templates):
      routes.append((t, cors_handler, ['OPTIONS']))

  # Add generic routes.
  routes.extend([
      directory_service_route(api_classes, base_path),
      discovery_service_route(api_classes, base_path),
      explorer_proxy_route(base_path),
      explorer_redirect_route(base_path),
  ])
  return routes


def api_server(api_classes, base_path='/_ah/api', regex='[^/]+'):
  """Creates a Flask application for the given Endpoints v1 services.

  Args:
    api_classes: A list of protorpc.remote.Service classes to create routes for.
    base_path: The base path under which all service paths should exist. If
      unspecified, defaults to /_ah/api.
    regex: Regular expression to allow in path parameters.

  Returns:
    A Flask applications.
  """
  return flask.Flask(api_routes(api_classes, base_path, regex))


def discovery_handler_factory(api_classes, base_path):
  """Returns a discovery request handler which knows about the given services.

  Args:
    api_classes: A list of protorpc.remote.Service classes the handler should
      know about.
    base_path: The base path under which all service paths exist.

  Returns:
    A Flask request handler function.
  """
  # Create a map of (name, version) => [services...].
  service_map = collections.defaultdict(list)
  for api_class in api_classes:
    service_map[(api_class.api_info.name,
                 api_class.api_info.version)].append(api_class)

  def discovery_handler(name, version):
    host = flask.request.headers['Host']
    services = service_map.get((name, version))
    if not services:
      flask.abort(404)

    return discovery.generate(services, host, base_path)

  return discovery_handler


def discovery_service_route(api_classes, base_path):
  """Returns a route to a handler which serves discovery documents.

  Args:
    api_classes: a list of protorpc.remote.Service classes the handler should
      know about.
    base_path: The base path under which all service paths exist.

  Returns:
    A tuple containing a URL string and a path.
  """
  return ('%s/discovery/v1/apis/<name>/<version>/rest' % base_path,
          discovery_handler_factory(api_classes, base_path))


def directory_handler_factory(api_classes, base_path):
  """Returns a directory request handler which knows about the given services.

  Args:
    api_classes: A list of protorpc.remote.Service classes the handler should
      know about.
    base_path: The base path under which all service paths exist.

  Returns:
    A Flask request handler function.
  """

  def directory_handler():
    host = flask.request.headers['Host']
    return discovery.directory(api_classes, host, base_path)

  return directory_handler


def directory_service_route(api_classes, base_path):
  """Returns a route to a handler which serves a directory list.

  Args:
    api_classes: A list of protorpc.remote.Service classes the handler should
      know about.
    base_path: The base path under which all service paths exist.

  Returns:
    A tuple containing a URL string and a path.
  """
  return ('%s/discovery/v1/apis' % base_path,
          directory_handler_factory(api_classes, base_path))


def explorer_proxy_route(base_path):
  """Returns a route to a handler which serves an API explorer proxy.

  Args:
    base_path: The base path under which all service paths exist.

  Returns:
    A tuple containing a URL string and a path.
  """

  def proxy_handler():
    """Returns a proxy capable of handling requests from API explorer."""

    return flask.render_template('adapter/proxy.html', base_path=base_path)

  template.bootstrap({
      'adapter': os.path.join(THIS_DIR, 'templates'),
  })

  return ('%s/static/proxy.html' % base_path, proxy_handler)


def explorer_redirect_route(base_path):
  """Returns a route to a handler which redirects to the API explorer.

  Args:
    base_path: The base path under which all service paths exist.

  Returns:
    A tuple containing a URL string and a Flask handler function.
  """

  def redirect_handler():
    """Returns a handler redirecting to the API explorer."""

    host = flask.request.headers['Host']
    flask.redirect('https://apis-explorer.appspot.com/apis-explorer'
                   '/?base=https://%s%s' % (host, base_path))

  return ('%s/explorer' % base_path, redirect_handler)
