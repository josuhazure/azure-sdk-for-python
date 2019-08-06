# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------
from __future__ import unicode_literals

import logging
import time

from uamqp import errors, constants, compat
from azure.eventhub.error import EventHubError, _handle_exception

log = logging.getLogger(__name__)


def _retry_decorator(to_be_wrapped_func):
    def wrapped_func(self, *args, **kwargs):
        timeout = kwargs.pop("timeout", 100000)
        if not timeout:
            timeout = 100000  # timeout equals to 0 means no timeout, set the value to be a large number.
        timeout_time = time.time() + timeout
        max_retries = self.client.config.max_retries
        retry_count = 0
        last_exception = None
        while True:
            try:
                return to_be_wrapped_func(self, timeout_time=timeout_time, last_exception=last_exception, **kwargs)
            except Exception as exception:
                last_exception = self._handle_exception(exception, retry_count, max_retries, timeout_time)
                retry_count += 1
    return wrapped_func


class ConsumerProducerMixin(object):
    def __init__(self):
        self.client = None
        self._handler = None
        self.name = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close(exc_val)

    def _check_closed(self):
        if self.error:
            raise EventHubError("{} has been closed. Please create a new one to handle event data.".format(self.name))

    def _create_handler(self):
        pass

    def _redirect(self, redirect):
        self.redirected = redirect
        self.running = False
        self._close_connection()

    def _open(self, timeout_time=None):
        """
        Open the EventHubConsumer using the supplied connection.
        If the handler has previously been redirected, the redirect
        context will be used to create a new handler before opening it.

        """
        # pylint: disable=protected-access
        if not self.running:
            if self._handler:
                self._handler.close()
            if self.redirected:
                alt_creds = {
                    "username": self.client._auth_config.get("iot_username"),
                    "password": self.client._auth_config.get("iot_password")}
            else:
                alt_creds = {}
            self._create_handler()
            self._handler.open(connection=self.client._conn_manager.get_connection(
                self.client.address.hostname,
                self.client.get_auth(**alt_creds)
            ))
            while not self._handler.client_ready():
                time.sleep(0.05)
            self._max_message_size_on_link = self._handler.message_handler._link.peer_max_message_size \
                                             or constants.MAX_MESSAGE_LENGTH_BYTES  # pylint: disable=protected-access
            self.running = True

    def _close_handler(self):
        self._handler.close()  # close the link (sharing connection) or connection (not sharing)
        self.running = False

    def _close_connection(self):
        self._close_handler()
        self.client._conn_manager.reset_connection_if_broken()

    def _handle_exception(self, exception, retry_count, max_retries, timeout_time):
        if not self.running and isinstance(exception, compat.TimeoutException):
            exception = errors.AuthenticationException("Authorization timeout.")
            return _handle_exception(exception, retry_count, max_retries, self, timeout_time)

        return _handle_exception(exception, retry_count, max_retries, self, timeout_time)

    def close(self, exception=None):
        # type:(Exception) -> None
        """
        Close down the handler. If the handler has already closed,
        this will be a no op. An optional exception can be passed in to
        indicate that the handler was shutdown due to error.

        :param exception: An optional exception if the handler is closing
         due to an error.
        :type exception: Exception

        Example:
            .. literalinclude:: ../examples/test_examples_eventhub.py
                :start-after: [START eventhub_client_receiver_close]
                :end-before: [END eventhub_client_receiver_close]
                :language: python
                :dedent: 4
                :caption: Close down the handler.

        """
        self.running = False
        if self.error:
            return
        if isinstance(exception, errors.LinkRedirect):
            self.redirected = exception
        elif isinstance(exception, EventHubError):
            self.error = exception
        elif exception:
            self.error = EventHubError(str(exception))
        else:
            self.error = EventHubError("{} handler is closed.".format(self.name))
        if self._handler:
            self._handler.close()  # this will close link if sharing connection. Otherwise close connection
