import click
import os
import yaml
import json
import logging

from ...util import api_request
from ..utils import run_command, run_commands, generate_run_id, git_temp_branch, git_get_current_branch, get_username
from ..context import current_context
import rebase as rb

def hpsearch(run_name=None, n_workers=4):
    context = current_context(from_file=True)

    logging.info(f"Starting hyperparam search: project: {context['project']}, experiment: {context['experiment']['name']}")

    current_dir = os.getcwd()
    repo_name = os.path.basename(current_dir)    
    if run_name is None:
        run_id = generate_run_id()
        run_name = f"hps-{run_id[:5]}"
    branch_name = f'HP-{run_name}'

    with git_temp_branch(branch_name, create=True) as new_branch:
        retc, _ = run_command("git diff-index --quiet HEAD")
        if retc == 1:
            run_command(f"git commit -a -m '{branch_name}'")        
        run_command(f"git push origin --set-upstream {branch_name}")

    run_commands([f"git rebase {branch_name}",
                  f"git reset --soft HEAD^",
                  f"git restore --staged ."], raise_error=True, return_output=True)

    retc, output = run_command("git config --get remote.origin.url", return_output=True)
    if retc != 0:
        raise RuntimeError(f"Could not extract git repo url: {output}")
    git_remote_url = output.strip()

    with open('hyperparams.yaml', 'r') as f:
        hyperparams = yaml.safe_load(f)
        data = {
            'hyperparams': hyperparams,
            'git_remote_url': f'{git_remote_url}',
            'branch_name': branch_name,
            'repo_name': repo_name,
            'run_name': run_name,
            'user': get_username(),
            'project_name': context['project'],
            'experiment_name': context['experiment']['name'],
            'api_key': rb.api_key,
            'n_workers': n_workers
        }

    r = api_request.post('platform/v1/model/hpsearch', data=json.dumps(data))
    logging.info(f"Status: {r.status_code}")
    data = r.json()
    logging.info('hpsearch id: {}'.format(data['hp_id']))

@click.command(name="hpsearch")
@click.option("--workers", "-w", "n_workers", default=4, type=int)
@click.option("--name", "-n", "run_name", default=None, type=str)
def hpsearch_cmd(*args, **kwargs):
    return hpsearch(*args, **kwargs)

__all__ = ['hpsearch', 'hpsearch_cmd']
