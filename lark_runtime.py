"""Select the deployment's CLI profile without changing the global default."""
import os


def command(domain, *arguments):
    profile = os.environ.get('LARK_CLI_PROFILE')
    return ['lark-cli', *(['--profile',profile] if profile else []), domain, *arguments]
