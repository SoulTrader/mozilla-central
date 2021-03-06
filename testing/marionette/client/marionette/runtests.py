# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from datetime import datetime
import imp
import inspect
import logging
from optparse import OptionParser
import os
import unittest
import socket
import sys
import time
import traceback
import platform
import moznetwork
import xml.dom.minidom as dom

from manifestparser import TestManifest
from mozhttpd import MozHttpd

from marionette import Marionette
from marionette_test import MarionetteJSTestCase, MarionetteTestCase


class MarionetteTestResult(unittest._TextTestResult):

    def __init__(self, *args, **kwargs):
        self.marionette = kwargs['marionette']
        del kwargs['marionette']
        super(MarionetteTestResult, self).__init__(*args, **kwargs)
        self.passed = 0
        self.tests_passed = []

    def addSuccess(self, test):
        super(MarionetteTestResult, self).addSuccess(test)
        self.passed += 1
        self.tests_passed.append(test)

    def getInfo(self, test):
        return test.test_name

    def getDescription(self, test):
        doc_first_line = test.shortDescription()
        if self.descriptions and doc_first_line:
            return '\n'.join((str(test), doc_first_line))
        else:
            desc = str(test)
            if hasattr(test, 'jsFile'):
                desc = "%s, %s" % (test.jsFile, desc)
            return desc

    def printLogs(self, test):
        for testcase in test._tests:
            if hasattr(testcase, 'loglines') and testcase.loglines:
                # Don't dump loglines to the console if they only contain
                # TEST-START and TEST-END.
                skip_log = True
                for line in testcase.loglines:
                    str_line = ' '.join(line)
                    if not 'TEST-END' in str_line and not 'TEST-START' in str_line:
                        skip_log = False
                        break
                if skip_log:
                    return
                self.stream.writeln('START LOG:')
                for line in testcase.loglines:
                    self.stream.writeln(' '.join(line).encode('ascii', 'replace'))
                self.stream.writeln('END LOG:')

    def printErrorList(self, flavour, errors):
        for test, err in errors:
            self.stream.writeln(self.separator1)
            self.stream.writeln("%s: %s" % (flavour, self.getDescription(test)))
            self.stream.writeln(self.separator2)
            errlines = err.strip().split('\n')
            for line in errlines[0:-1]:
                self.stream.writeln("%s" % line)
            if "TEST-UNEXPECTED-FAIL" in errlines[-1]:
                self.stream.writeln(errlines[-1])
            else:
                self.stream.writeln("TEST-UNEXPECTED-FAIL | %s | %s" %
                                    (self.getInfo(test), errlines[-1]))

    def stopTest(self, *args, **kwargs):
        unittest._TextTestResult.stopTest(self, *args, **kwargs)
        if self.marionette.check_for_crash():
            # this tells unittest.TestSuite not to continue running tests
            self.shouldStop = True


class MarionetteTextTestRunner(unittest.TextTestRunner):

    resultclass = MarionetteTestResult

    def __init__(self, **kwargs):
        self.marionette = kwargs['marionette']
        del kwargs['marionette']
        unittest.TextTestRunner.__init__(self, **kwargs)

    def _makeResult(self):
        return self.resultclass(self.stream,
                                self.descriptions,
                                self.verbosity,
                                marionette=self.marionette)

    def run(self, test):
        "Run the given test case or test suite."
        result = self._makeResult()
        if hasattr(self, 'failfast'):
            result.failfast = self.failfast
        if hasattr(self, 'buffer'):
            result.buffer = self.buffer
        startTime = time.time()
        startTestRun = getattr(result, 'startTestRun', None)
        if startTestRun is not None:
            startTestRun()
        try:
            test(result)
        finally:
            stopTestRun = getattr(result, 'stopTestRun', None)
            if stopTestRun is not None:
                stopTestRun()
        stopTime = time.time()
        timeTaken = stopTime - startTime
        result.printErrors()
        result.printLogs(test)
        if hasattr(result, 'separator2'):
            self.stream.writeln(result.separator2)
        run = result.testsRun
        self.stream.writeln("Ran %d test%s in %.3fs" %
                            (run, run != 1 and "s" or "", timeTaken))
        self.stream.writeln()

        expectedFails = unexpectedSuccesses = skipped = 0
        try:
            results = map(len, (result.expectedFailures,
                                result.unexpectedSuccesses,
                                result.skipped))
        except AttributeError:
            pass
        else:
            expectedFails, unexpectedSuccesses, skipped = results

        infos = []
        if not result.wasSuccessful():
            self.stream.write("FAILED")
            failed, errored = map(len, (result.failures, result.errors))
            if failed:
                infos.append("failures=%d" % failed)
            if errored:
                infos.append("errors=%d" % errored)
        else:
            self.stream.write("OK")
        if skipped:
            infos.append("skipped=%d" % skipped)
        if expectedFails:
            infos.append("expected failures=%d" % expectedFails)
        if unexpectedSuccesses:
            infos.append("unexpected successes=%d" % unexpectedSuccesses)
        if infos:
            self.stream.writeln(" (%s)" % (", ".join(infos),))
        else:
            self.stream.write("\n")
        return result


class MarionetteTestRunner(object):

    def __init__(self, address=None, emulator=None, emulatorBinary=None,
                 emulatorImg=None, emulator_res='480x800', homedir=None,
                 app=None, bin=None, profile=None, autolog=False, revision=None,
                 es_server=None, rest_server=None, logger=None,
                 testgroup="marionette", noWindow=False, logcat_dir=None,
                 xml_output=None, repeat=0, gecko_path=None, testvars=None,
                 tree=None, type=None, device=None, symbols_path=None,
                 **kwargs):
        self.address = address
        self.emulator = emulator
        self.emulatorBinary = emulatorBinary
        self.emulatorImg = emulatorImg
        self.emulator_res = emulator_res
        self.homedir = homedir
        self.app = app
        self.bin = bin
        self.profile = profile
        self.autolog = autolog
        self.testgroup = testgroup
        self.revision = revision
        self.es_server = es_server
        self.rest_server = rest_server
        self.logger = logger
        self.noWindow = noWindow
        self.httpd = None
        self.baseurl = None
        self.marionette = None
        self.logcat_dir = logcat_dir
        self.xml_output = xml_output
        self.repeat = repeat
        self.gecko_path = gecko_path
        self.testvars = {}
        self.test_kwargs = kwargs
        self.tree = tree
        self.type = type
        self.device = device
        self.symbols_path = symbols_path

        if testvars:
            if not os.path.exists(testvars):
                raise Exception('--testvars file does not exist')

            import json
            with open(testvars) as f:
                self.testvars = json.loads(f.read())

        # set up test handlers
        self.test_handlers = []
        self.register_handlers()

        self.reset_test_stats()

        if self.logger is None:
            self.logger = logging.getLogger('Marionette')
            self.logger.setLevel(logging.INFO)
            self.logger.addHandler(logging.StreamHandler())

        if self.logcat_dir:
            if not os.access(self.logcat_dir, os.F_OK):
                os.mkdir(self.logcat_dir)

        # for XML output
        self.testvars['xml_output'] = self.xml_output
        self.results = []

    def reset_test_stats(self):
        self.passed = 0
        self.failed = 0
        self.todo = 0
        self.failures = []

    def start_httpd(self):
        host = moznetwork.get_ip()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("",0))
        port = s.getsockname()[1]
        s.close()
        self.baseurl = 'http://%s:%d/' % (host, port)
        self.logger.info('running webserver on %s' % self.baseurl)
        self.httpd = MozHttpd(host=host,
                              port=port,
                              docroot=os.path.join(os.path.dirname(__file__), 'www'))
        self.httpd.start()

    def start_marionette(self):
        assert(self.baseurl is not None)
        if self.bin:
            if self.address:
                host, port = self.address.split(':')
            else:
                host = 'localhost'
                port = 2828
            self.marionette = Marionette(host=host,
                                         port=int(port),
                                         app=self.app,
                                         bin=self.bin,
                                         profile=self.profile,
                                         baseurl=self.baseurl)
        elif self.address:
            host, port = self.address.split(':')
            if self.emulator:
                self.marionette = Marionette.getMarionetteOrExit(
                                             host=host, port=int(port),
                                             connectToRunningEmulator=True,
                                             homedir=self.homedir,
                                             baseurl=self.baseurl,
                                             logcat_dir=self.logcat_dir,
                                             gecko_path=self.gecko_path,
                                             symbols_path=self.symbols_path)
            else:
                self.marionette = Marionette(host=host,
                                             port=int(port),
                                             baseurl=self.baseurl)
        elif self.emulator:
            self.marionette = Marionette.getMarionetteOrExit(
                                         emulator=self.emulator,
                                         emulatorBinary=self.emulatorBinary,
                                         emulatorImg=self.emulatorImg,
                                         emulator_res=self.emulator_res,
                                         homedir=self.homedir,
                                         baseurl=self.baseurl,
                                         noWindow=self.noWindow,
                                         logcat_dir=self.logcat_dir,
                                         gecko_path=self.gecko_path,
                                         symbols_path=self.symbols_path)
        else:
            raise Exception("must specify binary, address or emulator")

    def post_to_autolog(self, elapsedtime):
        self.logger.info('posting results to autolog')

        logfile = None
        if self.emulator:
            filename = os.path.join(os.path.abspath(self.logcat_dir),
                                    "emulator-%d.log" % self.marionette.emulator.port)
            if os.access(filename, os.F_OK):
                logfile = filename

        # This is all autolog stuff.
        # See: https://wiki.mozilla.org/Auto-tools/Projects/Autolog
        from mozautolog import RESTfulAutologTestGroup
        testgroup = RESTfulAutologTestGroup(
            testgroup = self.testgroup,
            os = 'android',
            platform = 'emulator',
            harness = 'marionette',
            server = self.es_server,
            restserver = self.rest_server,
            machine = socket.gethostname(),
            logfile = logfile)

        testgroup.set_primary_product(
            tree = self.tree,
            buildtype = 'opt',
            revision = self.revision)

        testgroup.add_test_suite(
            testsuite = 'b2g emulator testsuite',
            elapsedtime = elapsedtime.seconds,
            cmdline = '',
            passed = self.passed,
            failed = self.failed,
            todo = self.todo)

        # Add in the test failures.
        for f in self.failures:
            testgroup.add_test_failure(test=f[0], text=f[1], status=f[2])

        testgroup.submit()

    def run_tests(self, tests):
        self.reset_test_stats()
        starttime = datetime.utcnow()
        while self.repeat >=0:
            for test in tests:
                self.run_test(test)
            self.repeat -= 1
        self.logger.info('\nSUMMARY\n-------')
        self.logger.info('passed: %d' % self.passed)
        self.logger.info('failed: %d' % self.failed)
        self.logger.info('todo: %d' % self.todo)
        try:
            self.marionette.check_for_crash()
        except:
            traceback.print_exc()

        self.elapsedtime = datetime.utcnow() - starttime
        if self.autolog:
            self.post_to_autolog(self.elapsedtime)

        if self.xml_output:
            xml_dir = os.path.dirname(os.path.abspath(self.xml_output))
            if not os.path.exists(xml_dir):
                os.makedirs(xml_dir)
            with open(self.xml_output, 'w') as f:
                f.write(self.generate_xml(self.results))

        if self.marionette.instance:
            self.marionette.instance.close()
            self.marionette.instance = None
        del self.marionette

    def run_test(self, test):
        if not self.httpd:
            print "starting httpd"
            self.start_httpd()

        if not self.marionette:
            self.start_marionette()

        filepath = os.path.abspath(test)

        if os.path.isdir(filepath):
            for root, dirs, files in os.walk(filepath):
                for filename in files:
                    if ((filename.startswith('test_') or filename.startswith('browser_')) and 
                        (filename.endswith('.py') or filename.endswith('.js'))):
                        filepath = os.path.join(root, filename)
                        self.run_test(filepath)
                        if self.marionette.check_for_crash():
                            return
            return

        mod_name,file_ext = os.path.splitext(os.path.split(filepath)[-1])

        testloader = unittest.TestLoader()
        suite = unittest.TestSuite()

        if file_ext == '.ini':
            testargs = {}
            if self.type is not None:
                testtypes = self.type.replace('+', ' +').replace('-', ' -').split()
                for atype in testtypes:
                    if atype.startswith('+'):
                        testargs.update({ atype[1:]: 'true' })
                    elif atype.startswith('-'):
                        testargs.update({ atype[1:]: 'false' })
                    else:
                        testargs.update({ atype: 'true' })

            manifest = TestManifest()
            manifest.read(filepath)

            manifest_tests = manifest.active_tests(disabled=False)

            for i in manifest.get(tests=manifest_tests, **testargs):
                self.run_test(i["path"])
                if self.marionette.check_for_crash():
                    return
            return

        self.logger.info('TEST-START %s' % os.path.basename(test))

        for handler in self.test_handlers:
            if handler.match(os.path.basename(test)):
                handler.add_tests_to_suite(mod_name,
                                           filepath,
                                           suite,
                                           testloader,
                                           self.marionette,
                                           self.testvars,
                                           **self.test_kwargs)
                break

        if suite.countTestCases():
            runner = MarionetteTextTestRunner(verbosity=3,
                                              marionette=self.marionette)
            results = runner.run(suite)
            self.results.append(results)

            self.failed += len(results.failures) + len(results.errors)
            if hasattr(results, 'skipped'):
                self.todo += len(results.skipped) + len(results.expectedFailures)
            self.passed += results.passed
            for failure in results.failures + results.errors:
                self.failures.append((results.getInfo(failure[0]), failure[1], 'TEST-UNEXPECTED-FAIL'))
            if hasattr(results, 'unexpectedSuccess'):
                self.failed += len(results.unexpectedSuccesses)
                for failure in results.unexpectedSuccesses:
                    self.failures.append((results.getInfo(failure[0]), failure[1], 'TEST-UNEXPECTED-PASS'))

    def register_handlers(self):
        self.test_handlers.extend([MarionetteTestCase, MarionetteJSTestCase])

    def cleanup(self):
        if self.httpd:
            self.httpd.stop()

    __del__ = cleanup

    def generate_xml(self, results_list):

        def _extract_xml(test, text='', result='passed'):
            cls_name = test.__class__.__name__

            testcase = doc.createElement('testcase')
            testcase.setAttribute('classname', cls_name)
            testcase.setAttribute('name', unicode(test).split()[0])
            testcase.setAttribute('time', str(test.duration))
            testsuite.appendChild(testcase)

            if result in ['failure', 'error', 'skipped']:
                f = doc.createElement(result)
                f.setAttribute('message', 'test %s' % result)
                f.appendChild(doc.createTextNode(text))
                testcase.appendChild(f)

        doc = dom.Document()

        testsuite = doc.createElement('testsuite')
        testsuite.setAttribute('name', 'Marionette')
        testsuite.setAttribute('time', str(self.elapsedtime.total_seconds()))
        testsuite.setAttribute('tests', str(sum([results.testsRun for
                                                 results in results_list])))

        def failed_count(results):
            count = len(results.failures)
            if hasattr(results, 'unexpectedSuccesses'):
                count += len(results.unexpectedSuccesses)
            return count

        testsuite.setAttribute('failures', str(sum([failed_count(results)
                                               for results in results_list])))
        testsuite.setAttribute('errors', str(sum([len(results.errors)
                                             for results in results_list])))
        if hasattr(results, 'skipped'):
            testsuite.setAttribute('skips', str(sum([len(results.skipped) +
                                                     len(results.expectedFailures)
                                                     for results in results_list])))

        for results in results_list:

            for tup in results.errors:
                _extract_xml(*tup, result='error')

            for tup in results.failures:
                _extract_xml(*tup, result='failure')

            if hasattr(results, 'unexpectedSuccesses'):
                for test in results.unexpectedSuccesses:
                    # unexpectedSuccesses is a list of Testcases only, no tuples
                    _extract_xml(test, text='TEST-UNEXPECTED-PASS', result='failure')

            if hasattr(results, 'skipped'):
                for tup in results.skipped:
                    _extract_xml(*tup, result='skipped')

            if hasattr(results, 'expectedFailures'):
                for tup in results.expectedFailures:
                    _extract_xml(*tup, result='skipped')

            for test in results.tests_passed:
                _extract_xml(test)

        doc.appendChild(testsuite)
        return doc.toprettyxml(encoding='utf-8')


class MarionetteTestOptions(OptionParser):

    def __init__(self, **kwargs):
        OptionParser.__init__(self, **kwargs)

        self.add_option('--autolog',
                        action='store_true',
                        dest='autolog',
                        default=False,
                        help='send test results to autolog')
        self.add_option('--revision',
                        action='store',
                        dest='revision',
                        help='git revision for autolog submissions')
        self.add_option('--testgroup',
                        action='store',
                        dest='testgroup',
                        help='testgroup names for autolog submissions')
        self.add_option('--emulator',
                        action='store',
                        dest='emulator',
                        choices=['x86', 'arm'],
                        help='if no --address is given, then the harness will launch a B2G emulator on which to run '
                             'emulator tests. if --address is given, then the harness assumes you are running an '
                             'emulator already, and will run the emulator tests using that emulator. you need to '
                             'specify which architecture to emulate for both cases')
        self.add_option('--emulator-binary',
                        action='store',
                        dest='emulatorBinary',
                        help='launch a specific emulator binary rather than launching from the B2G built emulator')
        self.add_option('--emulator-img',
                        action='store',
                        dest='emulatorImg',
                        help='use a specific image file instead of a fresh one')
        self.add_option('--emulator-res',
                        action='store',
                        dest='emulator_res',
                        type='str',
                        help='set a custom resolution for the emulator'
                             'Example: "480x800"')
        self.add_option('--no-window',
                        action='store_true',
                        dest='noWindow',
                        default=False,
                        help='when Marionette launches an emulator, start it with the -no-window argument')
        self.add_option('--logcat-dir',
                        dest='logcat_dir',
                        action='store',
                        help='directory to store logcat dump files')
        self.add_option('--address',
                        dest='address',
                        action='store',
                        help='host:port of running Gecko instance to connect to')
        self.add_option('--device',
                        dest='device',
                        action='store',
                        help='serial ID of a device to use for adb / fastboot')
        self.add_option('--type',
                        dest='type',
                        action='store',
                        default='browser+b2g',
                        help="the type of test to run, can be a combination of values defined in the manifest file; "
                             "individual values are combined with '+' or '-' characters. for example: 'browser+b2g' "
                             "means the set of tests which are compatible with both browser and b2g; 'b2g-qemu' means "
                             "the set of tests which are compatible with b2g but do not require an emulator. this "
                             "argument is only used when loading tests from manifest files")
        self.add_option('--homedir',
                        dest='homedir',
                        action='store',
                        help='home directory of emulator files')
        self.add_option('--app',
                        dest='app',
                        action='store',
                        help='application to use')
        self.add_option('--binary',
                        dest='bin',
                        action='store',
                        help='gecko executable to launch before running the test')
        self.add_option('--profile',
                        dest='profile',
                        action='store',
                        help='profile to use when launching the gecko process. if not passed, then a profile will be '
                             'constructed and used')
        self.add_option('--repeat',
                        dest='repeat',
                        action='store',
                        type=int,
                        default=0,
                        help='number of times to repeat the test(s)')
        self.add_option('-x', '--xml-output',
                        action='store',
                        dest='xml_output',
                        help='xml output')
        self.add_option('--gecko-path',
                        dest='gecko_path',
                        action='store',
                        help='path to b2g gecko binaries that should be installed on the device or emulator')
        self.add_option('--testvars',
                        dest='testvars',
                        action='store',
                        help='path to a json file with any test data required')
        self.add_option('--tree',
                        dest='tree',
                        action='store',
                        default='b2g',
                        help='the tree that the revision parameter refers to')
        self.add_option('--symbols-path',
                        dest='symbols_path',
                        action='store',
                        help='absolute path to directory containing breakpad symbols, or the url of a zip file containing symbols')

    def verify_usage(self):
        options, tests = self.parse_args()

        if not tests:
            print 'must specify one or more test files, manifests, or directories'
            sys.exit(1)

        if not options.emulator and not options.address and not options.bin:
            print 'must specify --binary, --emulator or --address'
            sys.exit(1)

        # default to storing logcat output for emulator runs
        if options.emulator and not options.logcat_dir:
            options.logcat_dir = 'logcat'

        # check for valid resolution string, strip whitespaces
        try:
            if options.emulator_res:
                dims = options.emulator_res.split('x')
                assert len(dims) == 2
                width = str(int(dims[0]))
                height = str(int(dims[1]))
                options.emulator_res = 'x'.join([width, height])
        except:
            raise ValueError('Invalid emulator resolution format. '
                             'Should be like "480x800".')


def startTestRunner(runner_class, options, tests):
    runner = runner_class(**vars(options))
    runner.run_tests(tests)
    return runner

def cli(runner_class=MarionetteTestRunner, parser_class=MarionetteTestOptions):
    parser = parser_class(usage='%prog [options] test_file_or_dir <test_file_or_dir> ...')
    parser.verify_usage()
    options, tests = parser.parse_args()

    runner = startTestRunner(runner_class, options, tests)
    if runner.failed > 0:
        sys.exit(10)

if __name__ == "__main__":
    cli()
