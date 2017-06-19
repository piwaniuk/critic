# -*- mode: python; encoding: utf-8 -*-
#
# Copyright 2012 Jens Lindström, Opera Software ASA
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License.  You may obtain a copy of
# the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.  See the
# License for the specific language governing permissions and limitations under
# the License.

import errno
import os
import socket
import subprocess
import time

import configuration
import auth
import dbutils

from extensions.extension import Extension
from textutils import json_encode, json_decode

def startProcess(flavor):
    executable = configuration.extensions.FLAVORS[flavor]["executable"]
    library = configuration.extensions.FLAVORS[flavor]["library"]

    process = subprocess.Popen(
        [executable, "critic-launcher.js"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=library)

    return process

class ProcessException(Exception):
    pass

class ProcessError(ProcessException):
    def __init__(self, message):
        super(ProcessError, self).__init__(
            "Failed to execute process: %s" % message)

class ProcessTimeout(ProcessException):
    def __init__(self, timeout):
        super(ProcessTimeout, self).__init__(
            "Process timed out after %d seconds" % timeout)

class ProcessFailure(ProcessException):
    def __init__(self, returncode, stderr):
        super(ProcessFailure, self).__init__(
            "Process returned non-zero exit status %d" % returncode)
        self.returncode = returncode
        self.stderr = stderr

def executeProcess(db, manifest, role_name, script, function, extension_id,
                   user_id, argv, timeout, stdin=None, rlimit_rss=256):
    # If |user_id| is not the same as |db.user|, then one user's access of the
    # system is triggering an extension on behalf of another user.  This will
    # for instance happen when one user is adding changes to a review,
    # triggering an extension filter hook set up by another user.
    #
    # In this case, we need to check that the other user can access the
    # extension.
    #
    # If |user_id| is the same as |db.user|, we need to use |db.profiles|, which
    # may contain a profile associated with an access token that was used to
    # authenticate the user.
    if user_id != db.user.id:
        user = dbutils.User.fromId(db, user_id)
        authentication_labels = auth.DATABASE.getAuthenticationLabels(user)
        profiles = [auth.AccessControlProfile.forUser(
            db, user, authentication_labels)]
    else:
        authentication_labels = db.authentication_labels
        profiles = db.profiles

    extension = Extension.fromId(db, extension_id)
    if not auth.AccessControlProfile.isAllowedExtension(
            profiles, "execute", extension):
        raise auth.AccessDenied("Access denied to extension: execute %s"
                                % extension.getKey())

    flavor = manifest.flavor

    if manifest.flavor not in configuration.extensions.FLAVORS:
        flavor = configuration.extensions.DEFAULT_FLAVOR

    stdin_data = "%s\n" % json_encode({
            "library_path": configuration.extensions.FLAVORS[flavor]["library"],
            "rlimit": { "rss": rlimit_rss },
            "hostname": configuration.base.HOSTNAME,
            "dbname": configuration.database.PARAMETERS["database"],
            "dbuser": configuration.database.PARAMETERS["user"],
            "git": configuration.executables.GIT,
            "python": configuration.executables.PYTHON,
            "python_path": "%s:%s" % (configuration.paths.CONFIG_DIR,
                                      configuration.paths.INSTALL_DIR),
            "repository_work_copy_path": configuration.extensions.WORKCOPY_DIR,
            "changeset_address": configuration.services.CHANGESET["address"],
            "branchtracker_pid_path": configuration.services.BRANCHTRACKER["pidfile_path"],
            "maildelivery_pid_path": configuration.services.MAILDELIVERY["pidfile_path"],
            "is_development": configuration.debug.IS_DEVELOPMENT,
            "extension_path": manifest.path,
            "extension_id": extension_id,
            "user_id": user_id,
            "authentication_labels": list(authentication_labels),
            "role": role_name,
            "script_path": script,
            "fn": function,
            "argv": argv })

    if stdin is not None:
        stdin_data += stdin

    # Double the timeout. Timeouts are primarily handled by the extension runner
    # service, which returns an error response on timeout. This deadline here is
    # thus mostly to catch the extension runner service itself timing out.
    deadline = time.time() + timeout * 2

    try:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(max(0, deadline - time.time()))
        connection.connect(configuration.services.EXTENSIONRUNNER["address"])
        connection.sendall(json_encode({
            "stdin": stdin_data,
            "flavor": flavor,
            "timeout": timeout
        }))
        connection.shutdown(socket.SHUT_WR)

        data = ""

        while True:
            connection.settimeout(max(0, deadline - time.time()))
            try:
                received = connection.recv(4096)
            except socket.error as error:
                if error.errno == errno.EINTR:
                    continue
                raise
            if not received:
                break
            data += received

        connection.close()
    except socket.timeout as error:
        raise ProcessTimeout(timeout)
    except socket.error as error:
        raise ProcessError("failed to read response: %s" % error)

    try:
        data = json_decode(data)
    except ValueError as error:
        raise ProcessError("failed to decode response: %s" % error)

    if data["status"] == "timeout":
        raise ProcessTimeout(timeout)

    if data["status"] == "error":
        raise ProcessError(data["error"])

    if data["returncode"] != 0:
        raise ProcessFailure(data["returncode"], data["stderr"])

    return data["stdout"]
