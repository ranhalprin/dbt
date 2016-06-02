import argparse
import os.path
import sys
import dbt.project as project
import dbt.task.run as run_task
import dbt.task.compile as compile_task
import dbt.task.debug as debug_task
import dbt.task.clean as clean_task
import dbt.task.deps as deps_task
import dbt.task.init as init_task
import dbt.task.test as test_task

def main(args=None):
    if args is None:
        args = sys.argv[1:]

    p = argparse.ArgumentParser(prog='dbt: data build tool')
    subs = p.add_subparsers()

    base_subparser = argparse.ArgumentParser(add_help=False)
    base_subparser.add_argument('--profile', default=["user"], nargs='+', type=str, help='Which profile to load')

    sub = subs.add_parser('init', parents=[base_subparser])
    sub.add_argument('project_name', type=str, help='Name of the new project')
    sub.set_defaults(cls=init_task.InitTask, which='init')

    sub = subs.add_parser('clean', parents=[base_subparser])
    sub.set_defaults(cls=clean_task.CleanTask, which='clean')

    sub = subs.add_parser('compile', parents=[base_subparser])
    sub.set_defaults(cls=compile_task.CompileTask, which='compile')

    sub = subs.add_parser('debug', parents=[base_subparser])
    sub.set_defaults(cls=debug_task.DebugTask, which='debug')

    sub = subs.add_parser('deps', parents=[base_subparser])
    sub.set_defaults(cls=deps_task.DepsTask, which='deps')

    sub = subs.add_parser('run', parents=[base_subparser])
    sub.set_defaults(cls=run_task.RunTask, which='run')

    sub = subs.add_parser('test', parents=[base_subparser])
    sub.set_defaults(cls=test_task.TestTask, which='test')

    parsed = p.parse_args(args)

    if parsed.which == 'init':
        # bypass looking for a project file if we're running `dbt init`
        parsed.cls(args=parsed).run()

    elif os.path.isfile('dbt_project.yml'):
        proj = project.read_project('dbt_project.yml').with_profiles(parsed.profile)
        parsed.cls(args=parsed, project=proj).run()

    else:
        raise RuntimeError("dbt must be run from a project root directory with a dbt_project.yml file")

