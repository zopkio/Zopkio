# Copyright 2014 LinkedIn Corp.
#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""
Runs tests.
"""

import logging
import time
import traceback
import webbrowser

from naarad import Naarad

import dtf.constants as constants
import dtf.error_messages as error_messages
from dtf.reporter import Reporter
import dtf.runtime as runtime
import dtf.test_runner_helper as test_runner_helper
import dtf.utils as utils

logger = logging.getLogger(__name__)

class FailureHandler(object):
  """
  Maintains failure state to manage what to do after a non-test failure occurs
  """
  _NO_ABORT = -1
  _DEFAULT_FAILURES_BEFORE_ABORT = 2

  def __init__(self, failures_before_abort=None):
    if failures_before_abort is not None:
      self._failures_before_abort = failures_before_abort
    else:
      self._failures_before_abort = FailureHandler._DEFAULT_FAILURES_BEFORE_ABORT

    self._failure_count = 0

  def notify_failure(self):
    self._failure_count += 1

  def notify_success(self):
    self._failure_count = 0

  def get_abort_status(self):
    if (self._failures_before_abort != FailureHandler._NO_ABORT) and (self._failure_count > self._failures_before_abort):
      return False
    return True


class TestRunner(object):
  """
  Runs tests with the information given in the testfile
  """
  def __init__(self, testfile, tests_to_run, config_overrides):
    self.testfile = testfile
    self.deployment_module, self.perf_module, self.tests, self.master_config, self.configs = \
        test_runner_helper.get_modules(testfile, tests_to_run, config_overrides)

    self.directory_info = None
    self.reporter = None

  def run(self):
    """
    This is the main executable function that will run the test
    """
    self._setup()
    failure_handler = FailureHandler(self.master_config.mapping.get("max_suite_failures_before_abort"))

    naarad_obj = Naarad()
    for config in self.configs:
      self._reset_tests()
      if not failure_handler.get_abort_status():
        config.result = constants.SKIPPED
        config.message += error_messages.CONFIG_ABORT
        self._skip_all_tests()
        logger.debug("Skipping " + config.name + "due to too many setup_suite/teardown_suite failures")
      else:
        runtime.set_active_config(config)
        setup_fail = False
        config.naarad_id = naarad_obj.signal_start(self.perf_module.naarad_config(config.mapping))
        config.start_time = time.time()

        logger.debug("Setting up configuration: " + config.name)
        try:
          if hasattr(self.deployment_module, 'setup_suite'):
            self.deployment_module.setup_suite()
        except BaseException:
          config.result = constants.SKIPPED
          config.message += error_messages.SETUP_SUITE_FAILED + traceback.format_exc()
          self._skip_all_tests()
          setup_fail = True
          failure_handler.notify_failure()
          logger.debug("Aborting {0} due to setup_suite failure:\n{1}".format(config.name, traceback.format_exc()))
        else:
          logger.debug("Running tests for configuration: " + config.name)
          self._execute_run(config, naarad_obj)
          self._copy_logs()
          naarad_obj.signal_stop(config.naarad_id)
          self._execute_performance(naarad_obj)
          self._execute_verification()

        logger.debug("Tearing down configuration: " + config.name)
        try:
          if hasattr(self.deployment_module, 'teardown_suite'):
            self.deployment_module.teardown_suite()
          if not setup_fail:
            failure_handler.notify_success()
        except BaseException:
          config.message += error_messages.TEARDOWN_SUITE_FAILED + traceback.format_exc()
          if not setup_fail:
            failure_handler.notify_failure()
          logger.debug("{0} failed teardown_suite(). {1}".format(config.name, traceback.format_exc()))
        config.end_time = time.time()
        logger.debug("Execution of configuration: {0} complete".format(config.name))

      runtime.get_collector().collect(config, self.tests)
      # prints result to standard out - delete/comment out when things are working
      # self._print_debug()

    # analysis.generate_diff_reports()
    self.reporter.generate()

    if not self.master_config.mapping.get("no-display", False):
      self._display_results()

  def _convert_naarad_slas_to_list(self, naarad_sla_obj):
    """
    Returns a list of SLA objects

    :param naarad_sla_obj: the object returned by get_sla_data from the naarad API
    """
    sla_objs = []

    for a in naarad_sla_obj.values():
      for b in a.values():
        for c in b.values():
          for sla_obj in c.values():
            sla_objs.append(sla_obj)

    return sla_objs

  def _copy_logs(self):
    """
    Copy logs from remote machines to local destination
    """
    utils.makedirs(self.perf_module.LOGS_DIRECTORY)
    for deployer in runtime.get_deployers():
      for process in deployer.get_processes():
        logs = self.perf_module.machine_logs()[process.unique_id] + self.perf_module.naarad_logs()[process.unique_id]
        deployer.get_logs(process.unique_id, logs, self.perf_module.LOGS_DIRECTORY)

  def _execute_performance(self, naarad_obj):
    """
    Executes naarad

    :param naarad_obj:
    :return:
    """
    naarad_obj.analyze(self.perf_module.LOGS_DIRECTORY, self.perf_module.OUTPUT_DIRECTORY)

    for test in self.tests:
      if test.naarad_id is not None:
        test.naarad_stats = naarad_obj.get_stats_data(test.naarad_id)
        test.sla_objs = self._convert_naarad_slas_to_list(naarad_obj.get_sla_data(test.naarad_id))

  def _execute_run(self, config, naarad_obj):
    """
    Executes tests for a single config
    """
    failure_handler = FailureHandler(config.mapping.get("max_failures_per_suite_before_abort"))
    for test in self.tests:
      if not failure_handler.get_abort_status():
        test.result = constants.SKIPPED
        test.message += error_messages.TEST_ABORT
        logger.debug("Skipping" + test.name + "due to too many setup/teardown failures")
      else:
        setup_fail = False
        test.naarad_config = self.perf_module.naarad_config(config.mapping, test_name=test.name)
        test.naarad_id = naarad_obj.signal_start(test.naarad_config)
        test.start_time = time.time()
        logger.debug("Setting up test: " + test.name)
        try:
          if hasattr(self.deployment_module, 'setup'):
            self.deployment_module.setup()
        except BaseException:
          test.result = constants.SKIPPED
          test.message += error_messages.SETUP_FAILED + traceback.format_exc()
          setup_fail = True
          failure_handler.notify_failure()
          logger.debug("Aborting {0} due to setup failure:\n{1}".format(test.name, traceback.format_exc()))
        else:
          logger.debug("Executing test: " + test.name)
          try:
            test.func_start_time = time.time()
            test.function()
            test.func_end_time = time.time()
            test.result = constants.PASSED
          except BaseException as e:
            test.result = constants.FAILED
            test.exception = e
            test.message = traceback.format_exc()
        logger.debug("Tearing down test: " + test.name)
        try:
          if hasattr(self.deployment_module, 'teardown'):
            self.deployment_module.teardown()
          if not setup_fail:
            failure_handler.notify_success()
        except BaseException:
          test.message += error_messages.TEARDOWN_FAILED + traceback.format_exc()
          if not setup_fail:
            failure_handler.notify_failure()
          logger.debug(test.name + "failed teardown():\n{0}".format(traceback.format_exc()))

        test.end_time = time.time()
        naarad_obj.signal_stop(test.naarad_id)
        logger.debug("Execution of test: " + test.name + " complete")

  def _execute_verification(self):
    """
    Executes verification methods for the tests

    :return:
    """
    for test in self.tests:
      if (test.result != constants.SKIPPED
              and test.validation_function is not None
              and hasattr(test.validation_function, '__call__')):
        try:
          test.validation_function()
        except BaseException as e:
          test.result = constants.FAILED
          test.exception = e

  def _display_results(self):
    """
    Displays the report in a web page

    :return:
    """
    webbrowser.open(self.reporter.get_report_location())

  def _get_reporter(self):
    """
    Gets a Report object used to display results

    :return:
    """
    reporter = Reporter(self.directory_info["report_name"], self.directory_info["results_dir"],
                        self.directory_info["logs_dir"], self.perf_module.OUTPUT_DIRECTORY)
    return reporter

  def _print_debug(self):
    for test in self.tests:
      print "{0}----{1}".format(test.name, test.result)
      if test.result == constants.FAILED:
        print traceback.format_exception_only(type(test.exception), test.exception)

  def _reset_tests(self):
    for test in self.tests:
      test.reset()

  def _setup(self):
    """
    Sets up output directories and the reporter

    :return:
    """
    self.directory_info = test_runner_helper.directory_setup(self.testfile, self.perf_module)
    self.reporter = self._get_reporter()
    runtime.set_active_tests(self.tests)

  def _skip_all_tests(self):
    for test in self.tests:
      test.result = constants.SKIPPED