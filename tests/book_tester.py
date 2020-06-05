#!/usr/bin/env python
# -*- coding: utf-8 -*-
from lxml import html
from getpass import getuser
import io
import os
import stat
import re
import subprocess
import time
import tempfile
from textwrap import wrap
import unittest

from write_to_file import write_to_file
from book_parser import (
    CodeListing,
    Command,
    Output,
    parse_listing,
)
from sourcetree import Commit, SourceTree
from update_source_repo import update_sources_for_chapter

PHANTOMJS_RUNNER = os.path.join(
    os.path.abspath(os.path.dirname(__file__)),
    'my-phantomjs-qunit-runner.js'
)
SLIMERJS_BINARY = os.path.join(
    os.path.abspath(os.path.dirname(__file__)),
    'slimerjs-0.9.0/slimerjs'
)


DO_SERVER_COMMANDS = True
if getuser() == 'jenkins':
    DO_SERVER_COMMANDS = False
if 'NO_SERVER_COMMANDS' in os.environ:
    DO_SERVER_COMMANDS = False



def contains(inseq, subseq):
    return any(
        inseq[pos : pos + len(subseq)] == subseq
        for pos in range(len(inseq) - len(subseq) + 1)
    )



def wrap_long_lines(text):
    paragraphs = text.split('\n')
    return '\n'.join(
        '\n'.join(wrap(p, 79, break_long_words=True, break_on_hyphens=False))
        for p in paragraphs
    )


def split_blocks(text):
    return [
        block.strip() for block in
        re.split(r'\n\n+|^.*\[\.\.\..*$', text, flags=re.MULTILINE)
    ]


def fix_test_dashes(output):
    return output.replace(' ' + '-' * 69, '-' * 70)


def strip_mock_ids(output):
    strip_mocks_with_names = re.sub(
        r"Mock name='(.+)' id='(\d+)'>",
        r"Mock name='\1' id='XX'>",
        output,
    )
    return re.sub(
        r"Mock id='(\d+)'>",
        r"Mock id='XX'>",
        strip_mocks_with_names,
    )

def strip_object_ids(output):
    return re.sub('0x([0-9a-f]+)>', '0xXX>', output)


def strip_migration_timestamps(output):
    return re.sub(r'00(\d\d)_auto_20\d{6}_\d{4}', r'00\1_auto_20XXXXXX_XXXX', output)


def strip_localhost_port(output):
    return re.sub(r'localhost:\d\d\d\d\d?', r'localhost:XXXX', output)


def strip_session_ids(output):
    return re.sub(r'^[a-z0-9]{32}$', r'xxx_session_id_xxx', output)


def standardise_assertionerror_none(output):
    return output.replace("AssertionError: None", "AssertionError")


def strip_git_hashes(output):
    fixed_indexes = re.sub(
        r"index .......\.\........ 100644",
        r"index XXXXXXX\.\.XXXXXXX 100644",
        output,
    )
    return re.sub(
        r"^[a-f0-9]{7} ",
        r"XXXXXXX ",
        fixed_indexes,
        flags=re.MULTILINE,
    )


def strip_callouts(output):
    minus_old_callouts = re.sub(
        r"^(.+)  <\d+>$",
        r"\1",
        output,
        flags=re.MULTILINE,
    )
    return re.sub(
        r"^(.+)  \(\d+\)$",
        r"\1",
        minus_old_callouts,
        flags=re.MULTILINE,
    )


def standardise_library_paths(output):
    return re.sub(
        r'(File ").+packages/', r'\1.../', output, flags=re.MULTILINE,
    )


def strip_test_speed(output):
    return re.sub(
        r"Ran (\d+) tests? in \d+\.\d\d\ds",
        r"Ran \1 tests in X.Xs",
        output,
    )

def strip_js_test_speed(output):
    return re.sub(
        r"Took \d+ms to run (\d+) tests. (\d+) passed, (\d+) failed.",
        r"Took XXms to run \1 tests. \2 passed, \3 failed.",
        output,
    )


def strip_bdd_test_speed(output):
    return re.sub(
        r"features/steps/(\w+).py:(\d+) \d+.\d\d\ds",
        r"features/steps/\1.py:\2 XX.XXXs",
        output,
    )


def strip_screenshot_timestamps(output):
    fixed = re.sub(
        r"window0-(201\d-\d\d-\d\dT\d\d\.\d\d\.\d?\d?)",
        r"window0-201X-XX-XXTXX.XX",
        output,
    )
    # this last is very specific to one listing in 19...
    fixed = re.sub(r"^\d\d\.html$", "XX.html", fixed, flags=re.MULTILINE)
    return fixed


SQLITE_MESSAGES = {
    'django.db.utils.IntegrityError: lists_item.list_id may not be NULL':
    'django.db.utils.IntegrityError: NOT NULL constraint failed: lists_item.list_id',

    'django.db.utils.IntegrityError: columns list_id, text are not unique':
    'django.db.utils.IntegrityError: UNIQUE constraint failed: lists_item.list_id,\nlists_item.text',

    'sqlite3.IntegrityError: columns list_id, text are not unique':
    'sqlite3.IntegrityError: UNIQUE constraint failed: lists_item.list_id,\nlists_item.text'
}


def fix_sqlite_messages(actual_text):
    fixed_text = actual_text
    for old_version, new_version in SQLITE_MESSAGES.items():
        fixed_text = fixed_text.replace(old_version, new_version)
    return fixed_text


def fix_jenkins_pixelsize(actual_text):
    # TODO: remove me when upgrading bootstrap
    return actual_text.replace('107.0 != 512', '106.5 != 512')


def fix_creating_database_line(actual_text):
    creating_db = "Creating test database for alias 'default'..."
    actual_lines = actual_text.split('\n')
    if creating_db in actual_lines:
        actual_lines.remove(creating_db)
        actual_lines.insert(0, creating_db)
        actual_text = '\n'.join(actual_lines)
    return actual_text



def fix_interactive_managepy_stuff(actual_text):
    return actual_text.replace(
        'Select an option: ', 'Select an option:\n',
    ).replace(
        '>>> ', '>>>\n',
    )



class ChapterTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.sourcetree = SourceTree()
        self.tempdir = self.sourcetree.tempdir
        self.processes = []
        self.pos = 0
        self.dev_server_running = False
        self.current_server_cd = None
        self.current_server_exports = {}


    def tearDown(self):
        self.sourcetree.cleanup()


    def parse_listings(self):
        base_dir = os.path.split(os.path.abspath(os.path.dirname(__file__)))[0]
        filename = self.chapter_name + '.html'
        with open(os.path.join(base_dir, filename), encoding='utf-8') as f:
            raw_html = f.read()
        parsed_html = html.fromstring(raw_html)
        all_nodes = parsed_html.cssselect('.exampleblock.sourcecode, div:not(.sourcecode) div.listingblock')
        listing_nodes = []
        for ix, node in enumerate(all_nodes):
            prev = all_nodes[ix - 1]
            if node not in list(prev.iterdescendants()):
                listing_nodes.append(node)

        self.listings = [p for n in listing_nodes for p in parse_listing(n)]


    def check_final_diff(self, ignore=None, diff=None):
        if diff is None:
            diff = self.run_command(Command(
                'git diff -w repo/{}'.format(self.chapter_name)
            ))
        try:
            print('checking final diff', diff)
        except io.BlockingIOError:
            pass
        self.assertNotIn('fatal:', diff)
        start_marker = 'diff --git a/\n'
        commit = Commit.from_diff(start_marker + diff)

        if ignore is None:
            if commit.lines_to_add:
                self.fail('Found lines to add in diff:\n{}'.format(commit.lines_to_add))
            if commit.lines_to_remove:
                self.fail('Found lines to remove in diff:\n{}'.format(commit.lines_to_remove))
            return

        if "moves" in ignore:
            ignore.remove("moves")
            difference_lines = commit.deleted_lines + commit.new_lines
        else:
            difference_lines = commit.lines_to_add + commit.lines_to_remove

        for line in difference_lines:
            if any(ignorable in line for ignorable in ignore):
                continue
            self.fail('Found divergent line in diff:\n{}'.format(line))


    def start_with_checkout(self):
        update_sources_for_chapter(self.chapter_name, self.previous_chapter)
        self.sourcetree.start_with_checkout(self.chapter_name, self.previous_chapter)
        # simulate virtualenv folder
        self.sourcetree.run_command('mkdir -p virtualenv/bin virtualenv/lib')


    def write_to_file(self, codelisting):
        self.assertEqual(
            type(codelisting), CodeListing,
            "passed a non-Codelisting to write_to_file:\n%s" % (codelisting,)
        )
        print('writing to file', codelisting.filename)
        write_to_file(codelisting, self.tempdir)


    def apply_patch(self, codelisting):
        tf = tempfile.NamedTemporaryFile(delete=False)
        tf.write(codelisting.contents.encode('utf8'))
        tf.write('\n'.encode('utf8'))
        tf.close()
        print('patch:\n', codelisting.contents)
        patch_output = self.run_command(
            Command('patch --fuzz=3 --no-backup-if-mismatch %s %s' % (codelisting.filename, tf.name))
        )
        print(patch_output)
        self.assertNotIn('malformed', patch_output)
        self.assertNotIn('failed', patch_output.lower())
        codelisting.was_checked = True
        with open(os.path.join(self.tempdir, codelisting.filename)) as f:
            print(f.read())
        os.remove(tf.name)
        self.pos += 1
        codelisting.was_written = True


    def run_command(self, command, cwd=None, user_input=None, ignore_errors=False):
        self.assertEqual(
            type(command), Command,
            "passed a non-Command to run-command:\n%s" % (command,)
        )
        if command == 'git push':
            command.was_run = True
            return
        print('running command', command)
        output = self.sourcetree.run_command(command, cwd=cwd, user_input=user_input, ignore_errors=ignore_errors)
        command.was_run = True
        return output


    def _cleanup_runserver(self):
        self.run_server_command('pkill -f runserver', ignore_errors=True)


    RUN_SERVER_PATH = os.path.abspath(
        os.path.join(os.path.dirname(__file__), 'run_server_command.py')
    )

    def run_server_command(self, command, ignore_errors=False):
        sleep = 0
        kill_old_runserver = False
        kill_old_gunicorn = False

        cd_finder = re.compile(r'cd (.+)$')
        if cd_finder.match(command):
            self.current_server_cd = cd_finder.match(command).group(1)
            print('adding server cd', self.current_server_cd)
        export_finder = re.compile(r'export ([^=]+=\S+) ?([^=]+=\S+)?')
        if export_finder.match(command):
            for export_pair in export_finder.match(command).groups():
                if export_pair is not None:
                    key, val = export_pair.split('=')
                    self.current_server_exports[key] = val
                    print('adding server export', key, val)
        if 'unset' in command:
            del self.current_server_exports['DJANGO_SECRET_KEY']
            del self.current_server_exports['DJANGO_DEBUG_FALSE']
        if command == 'set -a; source .env; set +a':
            self.current_server_exports['DJANGO_SECRET_KEY'] = 'abc231'
            self.current_server_exports['DJANGO_DEBUG_FALSE'] = 'y'

        if command.startswith('sudo apt install '):
            command = command.replace('apt install ', 'apt install -y ')
            sleep = 1
        if command.startswith('sudo add-apt-repository'):
            command = command.replace('add-apt-repository ', 'apt-add-repository -y ')
            sleep = 1
        if command.startswith('sudo journalctl -f -u'):
            command = command.replace('journalctl -f -u', 'journalctl --no-pager -u')
        if command.startswith('git clone https://github.com/hjwp/book-example.git'):
            # hack for first git clone, use branch for manual chapter, and go back
            # one commit to simulate state near beginning of chap.
            command = command.replace(
                'git clone https://github.com/hjwp/book-example.git',
                'git clone -b chapter_manual_deployment https://github.com/hjwp/book-example.git'
            )
            command += ' && cd ~/sites/$SITENAME && git reset --hard HEAD^'

        if self.current_server_cd:
            command = f'cd {self.current_server_cd} && {command}'
        if self.current_server_exports:
            exports = ' '.join(f'{k}={v}' for k, v in self.current_server_exports.items())
            command = f'export {exports}; {command}'

        if 'manage.py runserver' in command:
            if './virtualenv/bin/python manage.py' in command:
                command = command.replace(
                    './virtualenv/bin/python manage.py runserver',
                    'dtach -n /tmp/dtach.sock ./virtualenv/bin/python manage.py runserver',
                )
                sleep = 1
                kill_old_runserver = True
            else:
                # special case first runserver errors
                ignore_errors = True
                command = command.replace('python manage.py', 'python3 manage.py')

        if 'bin/gunicorn' in command:
            kill_old_runserver = True
            kill_old_gunicorn = True
            command = command.replace(
                './virtualenv/bin/gunicorn',
                'dtach -n /tmp/dtach.sock ./virtualenv/bin/gunicorn',
            )

        if kill_old_runserver:
            subprocess.run([self.RUN_SERVER_PATH, 'pkill -f runserver'])
        if kill_old_gunicorn:
            subprocess.run([self.RUN_SERVER_PATH, 'pkill -f gunicorn'])

        print('running command on server', command)
        commands = [self.RUN_SERVER_PATH]
        if ignore_errors:
            commands.append('--ignore-errors')
        commands.append(command)
        output = subprocess.check_output(commands).decode('utf8')
        time.sleep(sleep)

        print(output.encode('utf-8'))
        return output


    def prep_virtualenv(self):
        virtualenv_path = os.path.join(self.tempdir, 'virtualenv')
        if not os.path.exists(virtualenv_path):
            print('preparing virtualenv')
            self.sourcetree.run_command(
                'python3.6 -m venv virtualenv'
            )
            self.sourcetree.run_command(
                './virtualenv/bin/python -m pip install -r requirements.txt'
            )


    def prep_database(self):
        self.sourcetree.run_command('python manage.py migrate --noinput')


    def write_file_on_server(self, target, contents):
        if not DO_SERVER_COMMANDS:
            return
        with tempfile.NamedTemporaryFile() as tf:
            tf.write(contents.encode('utf8'))
            tf.flush()
            output = subprocess.check_output(
                [self.RUN_SERVER_PATH, tf.name, target]
            ).decode('utf8')
            print(output)


    def assertLineIn(self, line, lines):
        if line not in lines:
            raise AssertionError('%s not found in:\n%s' % (
                repr(line), '\n'.join(repr(l) for l in lines))
            )

    def assert_console_output_correct(self, actual, expected, ls=False):
        print('checking expected output', expected)
        print('against actual', actual)
        self.assertEqual(
            type(expected), Output,
            "passed a non-Output to run-command:\n%s" % (expected,)
        )

        if self.tempdir in actual:
            actual = actual.replace(self.tempdir, '...python-tdd-book')
            actual = actual.replace('/private', '')  # macos thing

        if ls:
            actual = actual.strip()
            self.assertCountEqual(actual.split('\n'), expected.split())
            expected.was_checked = True
            return

        actual_fixed = standardise_library_paths(actual)
        actual_fixed = wrap_long_lines(actual_fixed)
        actual_fixed = strip_test_speed(actual_fixed)
        actual_fixed = strip_js_test_speed(actual_fixed)
        actual_fixed = strip_bdd_test_speed(actual_fixed)
        actual_fixed = strip_git_hashes(actual_fixed)
        actual_fixed = strip_mock_ids(actual_fixed)
        actual_fixed = strip_object_ids(actual_fixed)
        actual_fixed = strip_migration_timestamps(actual_fixed)
        actual_fixed = strip_session_ids(actual_fixed)
        actual_fixed = strip_localhost_port(actual_fixed)
        actual_fixed = strip_screenshot_timestamps(actual_fixed)
        actual_fixed = fix_sqlite_messages(actual_fixed)
        # actual_fixed = fix_jenkins_pixelsize(actual_fixed)
        actual_fixed = fix_creating_database_line(actual_fixed)
        actual_fixed = fix_interactive_managepy_stuff(actual_fixed)
        actual_fixed = standardise_assertionerror_none(actual_fixed)

        expected_fixed = standardise_library_paths(expected)
        expected_fixed = fix_test_dashes(expected_fixed)
        expected_fixed = strip_test_speed(expected_fixed)
        expected_fixed = strip_js_test_speed(expected_fixed)
        expected_fixed = strip_bdd_test_speed(expected_fixed)
        expected_fixed = strip_git_hashes(expected_fixed)
        expected_fixed = strip_mock_ids(expected_fixed)
        expected_fixed = strip_object_ids(expected_fixed)
        expected_fixed = strip_migration_timestamps(expected_fixed)
        expected_fixed = strip_session_ids(expected_fixed)
        expected_fixed = strip_localhost_port(expected_fixed)
        expected_fixed = strip_screenshot_timestamps(expected_fixed)
        expected_fixed = strip_callouts(expected_fixed)
        expected_fixed = standardise_assertionerror_none(expected_fixed)

        actual_fixed = actual_fixed.replace('\xa0', ' ')
        expected_fixed = expected_fixed.replace('\xa0', ' ')
        if '\t' in actual_fixed:
            actual_fixed = re.sub(r'\s+', ' ', actual_fixed)
            expected_fixed = re.sub(r'\s+', ' ', expected_fixed)

        actual_lines = actual_fixed.split('\n')
        expected_lines = expected_fixed.split('\n')

        for line in expected_lines:
            if line.startswith('[...'):
                continue
            if line.endswith('[...]'):
                line = line.rsplit('[...]')[0].rstrip()
                self.assertLineIn(line, [l[:len(line)] for l in actual_lines])
            elif line.startswith(' '):
                self.assertLineIn(line, actual_lines)
            else:
                self.assertLineIn(line, [l.strip() for l in actual_lines])

        if (
            len(expected_lines) > 4
            and '[...' not in expected_fixed
            and expected.type != 'qunit output'
        ):
            self.assertMultiLineEqual(actual_fixed.strip(), expected_fixed.strip())

        expected.was_checked = True


    def skip_with_check(self, pos, expected_content):
        listing = self.listings[pos]
        all_listings = '\n'.join(str(t) for t in enumerate(self.listings))
        error = f'Could not find {expected_content} at pos {pos}: "{listing}". Listings were:\n{all_listings}'
        if hasattr(listing, 'contents'):
            if expected_content not in listing.contents:
                raise Exception(error)
        else:
            if expected_content not in listing:
                raise Exception(error)
        listing.skip = True


    def replace_command_with_check(self, pos, old, new):
        listing = self.listings[pos]
        all_listings = '\n'.join(str(t) for t in enumerate(self.listings))
        error = f'Could not find {old} at pos {pos}: "{listing}". Listings were:\n{all_listings}'
        if old not in listing:
            raise Exception(error)
        assert type(listing) == Command

        new_listing = Command(listing.replace(old, new))
        for attr, val in vars(listing).items():
            setattr(new_listing, attr, val)
        self.listings[pos] = new_listing



    def assert_directory_tree_correct(self, expected_tree, cwd=None):
        actual_tree = self.sourcetree.run_command('tree -I *.pyc --noreport', cwd)
        self.assert_console_output_correct(actual_tree, expected_tree)


    def assert_all_listings_checked(self, listings, exceptions=[]):
        for i, listing in enumerate(listings):
            if i in exceptions:
                continue
            if listing.skip:
                continue

            if type(listing) == CodeListing:
                self.assertTrue(
                    listing.was_written,
                    'Listing %d not written:\n%s' % (i, listing)
                )
            if type(listing) == Command:
                self.assertTrue(
                    listing.was_run,
                    'Command %d not run:\n%s' % (i, listing)
                )
            if type(listing) == Output:
                self.assertTrue(
                    listing.was_checked,
                    'Output %d not checked:\n%s' % (i, listing)
                )


    def check_test_code_cycle(self, pos, test_command_in_listings=True, ft=False):
        self.write_to_file(self.listings[pos])
        self._strip_out_any_pycs()
        if test_command_in_listings:
            pos += 1
            self.assertIn('test', self.listings[pos])
            test_run = self.run_command(self.listings[pos])
        elif ft:
            test_run = self.run_command(Command("python functional_tests.py"))
        else:
            test_run = self.run_command(Command("python manage.py test lists"))
        pos += 1
        self.assert_console_output_correct(test_run, self.listings[pos])


    def unset_PYTHONDONTWRITEBYTECODE(self):
        # so any references to  __pycache__ in the book work
        if 'PYTHONDONTWRITEBYTECODE' in os.environ:
            del os.environ['PYTHONDONTWRITEBYTECODE']


    def _strip_out_any_pycs(self):
        return


    def run_test_and_check_result(self, bdd=False):
        if bdd:
            self.assertIn('behave', self.listings[self.pos])
        else:
            self.assertIn('test', self.listings[self.pos])
        self._strip_out_any_pycs()
        if bdd:
            test_run = self.run_command(self.listings[self.pos], ignore_errors=True)
        else:
            test_run = self.run_command(self.listings[self.pos])
        self.assert_console_output_correct(test_run, self.listings[self.pos + 1])
        self.pos += 2


    def run_js_tests(self, tests_path):
        output = subprocess.check_output(
            ['phantomjs', PHANTOMJS_RUNNER, tests_path]
        ).decode()
        # some fixes to make phantom more like firefox
        output = output.replace('at file', '@file')
        output = re.sub(r"Can't find variable: (\w+)", r"\1 is not defined", output)
        output = re.sub(
            r"'(\w+)' is not an object \(evaluating '(\w+)\.\w+'\)",
            r"\2 is \1",
            output
        )
        output = re.sub(
            r"'undefined' is not a function \(evaluating '(.+)\(.*\)'\)",
            r"\1 is not a function",
            output
        )
        print('fixed phantomjs output', output)
        return output


    def check_qunit_output(self, expected_output):
        lists_tests = os.path.join(
            self.tempdir,
            'lists/static/tests/tests.html'
        )
        accounts_tests_exist = os.path.exists(os.path.join(
            self.sourcetree.tempdir,
            'accounts/static/tests/tests.html'
        ))

        accounts_tests = lists_tests.replace('/lists/', '/accounts/')
        lists_run = self.run_js_tests(lists_tests)
        if not accounts_tests_exist:
            self.assert_console_output_correct(lists_run, expected_output)
            return
        else:
            if '0 failed' in lists_run and '0 failed' not in expected_output:
                print('lists tests pass, assuming accounts tests')
                accounts_run = self.run_js_tests(accounts_tests)
                self.assert_console_output_correct(accounts_run, expected_output)

            else:
                try:
                    self.assert_console_output_correct(lists_run, expected_output)
                except AssertionError as first_error:
                    if '0 failed' in lists_run and '0 failed' in expected_output:
                        print('lists and expected both had 0 failed but didnt match. checking accounts')
                        print('lists run was', lists_run)
                        accounts_run = self.run_js_tests(accounts_tests)
                        self.assert_console_output_correct(accounts_run, expected_output)
                    else:
                        raise first_error


    def check_current_contents(self, listing, actual_contents):
        print("CHECK CURRENT CONTENTS")
        stripped_actual_lines = [l.strip() for l in actual_contents.split('\n')]
        listing_contents = re.sub(r' +#$', '', listing.contents, flags=re.MULTILINE)
        for block in split_blocks(listing_contents):
            stripped_block = [line.strip() for line in block.strip().split('\n')]
            for line in stripped_block:
                self.assertIn(line, stripped_actual_lines)
            self.assertTrue(
                contains(stripped_actual_lines, stripped_block),
                '\n{}\n\nnot found in\n\n{}'.format('\n'.join(stripped_block), '\n'.join(stripped_actual_lines)),
            )
        listing.was_written = True


    def check_commit(self, pos):
        if self.listings[pos].endswith('commit -a'):
            self.listings[pos] = Command(
                self.listings[pos] + 'm "commit for listing %d"' % (self.pos,)
            )
        elif self.listings[pos].endswith('commit'):
            self.listings[pos] = Command(
                self.listings[pos] + ' -am "commit for listing %d"' % (self.pos,)
            )

        commit = self.run_command(self.listings[pos])
        assert 'insertion' in commit or 'changed' in commit
        self.pos += 1


    def check_diff_or_status(self, pos):
        LIKELY_FILES = [
            'urls.py', 'tests.py', 'views.py', 'functional_tests.py',
            'settings.py', 'home.html', 'list.html', 'base.html',
            'fabfile.py', 'tests/test_', 'base.py', 'test_my_lists.py'
        ]
        self.assertTrue(
            'diff' in self.listings[pos] or 'status' in self.listings[pos]
        )
        git_output = self.run_command(self.listings[pos])
        if all('/' + l not in git_output for l in LIKELY_FILES) and all(
            f not in git_output for f in ('lists/', 'functional_tests.py')
        ):
            self.fail('no likely files in diff output %s' % (git_output,))
        self.pos += 1
        comment = self.listings[pos + 1]
        if comment.skip:
            comment.was_checked = True
            self.pos += 1
            return
        if comment.type != 'output':
            return
        for expected_file in LIKELY_FILES:
            if '/' + expected_file in git_output:
                if expected_file not in comment:
                    self.fail(
                        "could not find %s in comment %r given git output\n%s" % (
                            expected_file, comment, git_output)
                    )
                self.listings[pos + 1].was_checked = True
        comment.was_checked = True
        self.pos += 1


    def check_git_diff_and_commit(self, pos):
        self.check_diff_or_status(pos)
        self.check_commit(pos + 2)


    def start_dev_server(self):
        self.run_command(Command('python manage.py runserver'))
        self.dev_server_running = True
        time.sleep(1)


    def restart_dev_server(self):
        print('restarting dev server')
        self.run_command(Command('pkill -f runserver'))
        time.sleep(1)
        self.start_dev_server()
        time.sleep(1)




    def run_unit_tests(self):
        if os.path.exists(os.path.join(self.tempdir, 'accounts', 'tests')):
            return self.run_command(Command("python manage.py test lists accounts"))
        else:
            return self.run_command(Command("python manage.py test lists"))


    def run_fts(self):
        if os.path.exists(os.path.join(self.tempdir, 'functional_tests')):
            return self.run_command(Command("python manage.py test functional_tests"))
        else:
            return self.run_command(Command("python functional_tests.py"))

    def run_interactive_manage_py(self, listing):
        output_before = self.listings[self.pos + 1]
        assert isinstance(output_before, Output)

        LIKELY_INPUTS = ('yes', 'no', '1', '2', "''")
        user_input = self.listings[self.pos + 2]
        if isinstance(user_input, Command) and user_input in LIKELY_INPUTS:
            if user_input == 'yes':
                print('yes case')
                # in this case there is moar output after the yes
                output_after = self.listings[self.pos + 3]
                assert isinstance(output_after, Output)
                expected_output = Output(wrap_long_lines(output_before + ' ' + output_after.lstrip()))
                next_output = None
            elif user_input == '1':
                print('migrations 1 case')
                # in this case there is another hop
                output_after = self.listings[self.pos + 3]
                assert isinstance(output_after, Output)
                first_input = user_input
                next_input = self.listings[self.pos + 4]
                assert isinstance(next_input, Command)
                next_output = self.listings[self.pos + 5]
                expected_output = Output(wrap_long_lines(
                    output_before + '\n' + output_after + '\n' + next_output
                ))
                user_input = Command(first_input + '\n' + next_input)
            else:
                expected_output = output_before
                output_after = None
                next_output = None
            ignore_errors = True if user_input == '2' else False
        else:
            user_input = None
            expected_output = output_before
            output_after = None
            ignore_errors = True
            next_output = None

        output = self.run_command(listing, user_input=user_input, ignore_errors=ignore_errors)
        self.assert_console_output_correct(output, expected_output)

        listing.was_checked = True
        output_before.was_checked = True
        self.pos += 2
        if user_input is not None:
            user_input.was_run = True
            self.pos += 1
        if output_after is not None:
            output_after.was_checked = True
            self.pos += 1
        if next_output is not None:
            self.pos += 2
            next_output.was_checked = True
            first_input.was_run = True
            next_input.was_run = True

    def recognise_listing_and_process_it(self):
        listing = self.listings[self.pos]
        if listing.dofirst:
            print("DOFIRST", listing.dofirst)
            self.sourcetree.patch_from_commit(
                listing.dofirst,
            )
        if listing.skip:
            print("SKIP")
            listing.was_checked = True
            listing.was_written = True
            self.pos += 1
        elif listing.against_server and not DO_SERVER_COMMANDS:
            print("SKIP AGAINST SERVER")
            listing.was_checked = True
            listing.was_run = True
            self.pos += 1
        elif listing.type == 'test':
            print("TEST RUN")
            self.run_test_and_check_result()
        elif listing.type == 'bdd test':
            print("BDD TEST RUN")
            self.run_test_and_check_result(bdd=True)
        elif listing.type == 'git diff':
            print("GIT DIFF")
            self.check_diff_or_status(self.pos)
        elif listing.type == 'git status':
            print("STATUS")
            self.check_diff_or_status(self.pos)
        elif listing.type == 'git commit':
            print("COMMIT")
            self.check_commit(self.pos)
        elif listing.type == 'interactive manage.py':
            print("INTERACTIVE MANAGE.PY")
            self.run_interactive_manage_py(listing)
        elif listing.type == 'tree':
            print("TREE")
            self.assert_directory_tree_correct(listing)
            self.pos += 1

        elif listing.type == 'server command':
            if DO_SERVER_COMMANDS:
                server_output = self.run_server_command(listing)
            listing.was_run = True
            self.pos += 1
            next_listing = self.listings[self.pos]
            if next_listing.type == 'output' and not next_listing.skip:
                if DO_SERVER_COMMANDS:
                    for line in next_listing.split('\n'):
                        line = line.split('[...]')[0].strip()
                        line = re.sub(r'\s+', ' ', line)
                        server_output = re.sub(r'\s+', ' ', server_output)
                        self.assertIn(line, server_output)
                next_listing.was_checked = True
                self.pos += 1

        elif listing.type == 'against staging':
            print("AGAINST STAGING")
            next_listing = self.listings[self.pos + 1]
            if DO_SERVER_COMMANDS:
                output = self.run_command(listing, ignore_errors=listing.ignore_errors)
                listing.was_checked = True
            else:
                listing.skip = True
            if next_listing.type == 'output' and not next_listing.skip:
                if DO_SERVER_COMMANDS:
                    self.assert_console_output_correct(output, next_listing)
                    next_listing.was_checked = True
                else:
                    next_listing.skip = True
                self.pos += 2

        elif listing.type == 'other command':
            print("A COMMAND")
            output = self.run_command(listing, ignore_errors=listing.ignore_errors)
            next_listing = self.listings[self.pos + 1]
            if next_listing.type == 'output' and not next_listing.skip:
                ls = listing.startswith('ls')
                self.assert_console_output_correct(output, next_listing, ls=ls)
                next_listing.was_checked = True
                listing.was_checked = True
                self.pos += 2
            elif 'tree' in listing and next_listing.type == 'tree':
                self.assert_console_output_correct(output, next_listing)
                next_listing.was_checked = True
                listing.was_checked = True
                self.pos += 2
            else:
                listing.was_checked = True
                self.pos += 1

        elif listing.type == 'diff':
            print("DIFF")
            self.apply_patch(listing)

        elif listing.type == 'code listing currentcontents':
            actual_contents = self.sourcetree.get_contents(
                listing.filename
            )
            self.check_current_contents(listing, actual_contents)
            self.pos += 1

        elif listing.type == 'code listing':
            print("CODE")
            self.write_to_file(listing)
            self.pos += 1

        elif listing.type == 'code listing with git ref':
            print("CODE FROM GIT REF")
            self.sourcetree.apply_listing_from_commit(listing)
            self.pos += 1

        elif listing.type == 'server code listing':
            print("SERVER CODE")
            self.write_file_on_server(listing.filename, listing.contents)
            listing.was_written = True
            self.pos += 1

        elif listing.type == 'qunit output':
            self.check_qunit_output(listing)
            self.pos += 1

        elif listing.type == 'output':
            self._strip_out_any_pycs()
            test_run = self.run_unit_tests()
            if 'OK' in test_run and 'OK' not in listing:
                print('unit tests pass, must be an FT:\n', test_run)
                test_run = self.run_fts()
            try:
                self.assert_console_output_correct(test_run, listing)
            except AssertionError as e:
                if 'OK' in test_run and 'OK' in listing:
                    print('got error when checking unit tests', e)
                    test_run = self.run_fts()
                    self.assert_console_output_correct(test_run, listing)
                else:
                    raise

            self.pos += 1

        else:
            self.fail('not implemented for ' + str(listing))

