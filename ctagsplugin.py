#!/usr/bin/env python

"""A ctags plugin for Sublime Text 2/3"""

import functools
import codecs
import os
import pprint
import re
import string
import threading

from itertools import chain
from operator import itemgetter as iget
from collections import defaultdict

try:
    import sublime
    import sublime_plugin
    from sublime import status_message, error_message
except ImportError:  # running tests
    import sys

    from tests.sublime_fake import sublime
    from tests.sublime_fake import sublime_plugin

    sys.modules['sublime'] = sublime
    sys.modules['sublime_plugin'] = sublime_plugin

if sublime.version().startswith('2'):
    import ctags
    from ctags import (FILENAME, parse_tag_lines, PATH_ORDER, SYMBOL, Tag,
                       TagFile)
    from helpers.edit import Edit
else:  # safe to assume if not ST2 then ST3
    from CTags import ctags
    from CTags.ctags import (FILENAME, parse_tag_lines, PATH_ORDER, SYMBOL,
                             Tag, TagFile)
    from CTags.helpers.edit import Edit

"""
Contants
"""

OBJECT_PUNCTUATORS = {
    'class': '.',
    'struct': '::',
    'function': '/',
}

ENTITY_SCOPE = 'entity.name.function, entity.name.type, meta.toc-list'

RUBY_SPECIAL_ENDINGS = '\?|!'
RUBY_SCOPES = '.*(ruby|rails).*'

ON_LOAD = sublime_plugin.all_callbacks['on_load']

RE_SPECIAL_CHARS = re.compile(
    '(\\\\|\\*|\\+|\\?|\\||\\{|\\}|\\[|\\]|\\(|\\)|\\^|\\$|\\.|\\#|\\ )')


"""
Functions
"""

"""Helper functions"""


def get_settings():
    return sublime.load_settings("CTags.sublime-settings")


def get_setting(key, default=None, view=None):
    try:
        if view is None:
            view = sublime.active_window().active_view()
        s = view.settings()
        if s.has('ctags_%s' % key):
            return s.get('ctags_%s' % key)
    except:
        pass
    return get_settings().get(key, default)

setting = get_setting


def escape_regex(s):
    return RE_SPECIAL_CHARS.sub(lambda m: '\\%s' % m.group(1), s)


def select(view, region):
    sel_set = view.sel()
    sel_set.clear()
    sel_set.add(region)
    view.show(region)


def in_main(f):
    @functools.wraps(f)
    def done_in_main(*args, **kw):
        sublime.set_timeout(functools.partial(f, *args, **kw), 0)

    return done_in_main


# TODO: allow thread per tag file. That makes more sense.
def threaded(finish=None, msg='Thread already running'):
    def decorator(func):
        func.running = 0

        @functools.wraps(func)
        def threaded(*args, **kwargs):
            def run():
                try:
                    result = func(*args, **kwargs)
                    if result is None:
                        result = ()

                    elif not isinstance(result, tuple):
                        result = (result, )

                    if finish:
                        sublime.set_timeout(
                            functools.partial(finish, args[0], *result), 0)
                finally:
                    func.running = 0
            if not func.running:
                func.running = 1
                t = threading.Thread(target=run)
                t.setDaemon(True)
                t.start()
            else:
                status_message(msg)
        threaded.func = func

        return threaded

    return decorator


def on_load(path=None, window=None, encoded_row_col=True, begin_edit=False):
    """Decorator to open or switch to a file.

    Opens and calls the "decorated function" for the file specified by path,
    or the current file if no path is specified. In the case of the former, if
    the file is open in another tab that tab will gain focus, otherwise the
    file will be opened in a new tab with a requisite delay to allow the file
    to open. In the latter case, the "decorated function" will be called on
    the currently open file.

    :param path: path to a file
    :param window: the window to open the file in
    :param encoded_row_col: the ``sublime.ENCODED_POSITION`` flag for
        ``sublime.Window.open_file``
    :param begin_edit: if editing the file being opened

    :returns: None
    """
    window = window or sublime.active_window()

    def wrapper(f):
        # if no path, tag is in current open file, return that
        if not path:
            return f(window.active_view())
        # else, open the relevant file
        view = window.open_file(os.path.normpath(path), encoded_row_col)

        def wrapped():
            # if editing the open file
            if begin_edit:
                with Edit(view):
                    f(view)
            else:
                f(view)

        # if buffer is still loading, wait for it to complete then proceed
        if view.is_loading():

            class set_on_load():
                callbacks = ON_LOAD

                def __init__(self):
                    # append self to callbacks
                    self.callbacks.append(self)

                def remove(self):
                    # remove self from callbacks, hence disconnecting it
                    self.callbacks.remove(self)

                def on_load(self, view):
                    # on file loading
                    try:
                        wrapped()
                    finally:
                        # disconnect callback
                        self.remove()

            set_on_load()
        # else just proceed (file was likely open already in another tab)
        else:
            wrapped()

    return wrapper


def find_tags_relative_to(file_name, tag_file):
    if not file_name:
        return None

    dirs = os.path.dirname(os.path.normpath(file_name)).split(os.path.sep)

    while dirs:
        joined = os.path.sep.join(dirs + [tag_file])

        if os.path.exists(joined) and not os.path.isdir(joined):
            return joined
        else:
            dirs.pop()

    return None


def alternate_tags_paths(view, tags_file):
    tags_paths = '%s_search_paths' % tags_file
    search_paths = [tags_file]

    if os.path.exists(tags_paths):
        search_paths.extend(
            codecs.open(tags_paths, encoding='utf-8').read().split('\n'))

    try:
        for (selector, platform), path in setting('extra_tag_paths'):
            if view.match_selector(view.sel()[0].begin(), selector):
                if sublime.platform() == platform:
                    search_paths.append(path)
    except Exception as e:
        print(e)

    if os.path.exists(tags_paths):
        for extrafile in setting('extra_tag_files'):
            search_paths.append(
                os.path.normpath(
                    os.path.join(os.path.dirname(tags_file), extrafile)))

    # ok, didn't found the tags file under the viewed file.
    # let's look in the currently opened folder
    for folder in view.window().folders():
        search_paths.append(
            os.path.normpath(
                os.path.join(folder, setting('tag_file'))))
        for extrafile in setting('extra_tag_files'):
            search_paths.append(
                os.path.normpath(
                    os.path.join(folder, extrafile)))

    return set(p for p in search_paths if p and os.path.exists(p))


def reached_top_level_folders(folders, oldpath, path):
    if oldpath == path:
        return True
    for folder in folders:
        if folder[:len(path)] == path:
            return True
        if path == os.path.dirname(folder):
            return True
    return False


def find_top_folder(folders, filename):
    path = os.path.dirname(filename)

    # we don't have any folders open, return the folder this file is in
    if len(folders) == 0:
        return path

    oldpath = ''
    while not reached_top_level_folders(folders, oldpath, path):
        oldpath = path
        path = os.path.dirname(path)
    return path


"""Scrolling functions"""


def find_with_scope(view, pattern, scope, start_pos=0, cond=True, flags=0):
    max_pos = view.size()

    while start_pos < max_pos:
        f = view.find(pattern[:-5] + '$', start_pos, flags)

        if not f or view.match_selector(f.begin(), scope) is cond:
            break
        else:
            start_pos = f.end()

    return f


def find_source(view, pattern, start_at, flags=sublime.LITERAL):
    return find_with_scope(view, pattern, 'comment,string',
                           start_at, False, flags)


def follow_tag_path(view, tag_path, pattern):
    regions = [sublime.Region(0, 0)]

    for p in list(tag_path)[1:-1]:
        while True:  # .end() is BUG!
            regions.append(find_source(view, p, regions[-1].begin()))

            if ((regions[-1] in (None, regions[-2]) or
                 view.match_selector(regions[-1].begin(), ENTITY_SCOPE))):
                regions = [r for r in regions if r is not None]
                break

    start_at = max(regions, key=lambda r: r.begin()).begin() - 1

    # find the ex_command pattern
    pattern_region = find_source(
        view, '^' + escape_regex(pattern), start_at, flags=0)

    if setting('debug'):  # leave a visual trail for easy debugging
        regions = regions + ([pattern_region] if pattern_region else [])
        view.erase_regions('tag_path')
        view.add_regions('tag_path', regions, 'comment', 1)

    return pattern_region.begin() - 1 if pattern_region else start_at


def scroll_to_tag(view, tag, hook=None):
    @on_load(os.path.join(tag.root_dir, tag.filename))
    def and_then(view):
        if tag.ex_command.isdigit():
            look_from = view.text_point(int(tag.ex_command)-1, 0)
        else:
            look_from = follow_tag_path(view, tag.tag_path, tag.ex_command)

        symbol_region = view.find(tag.ex_command, look_from, sublime.LITERAL)

        select(view, (symbol_region or (view.line(look_from + 1)
                      if look_from else sublime.Region(0, 0))))

        if hook:
            hook(view)


"""Formatting helper functions"""


def format_tag_for_quickopen(tag, show_path=True):
    """Format a tag for the quickopen panel"""
    format = []
    tag = ctags.Tag(tag)
    f = ''

    for field in getattr(tag, 'field_keys', []):
        if field in PATH_ORDER:
            punct = OBJECT_PUNCTUATORS.get(field, ' -> ')
            f += string.Template(
                '    %($field)s$punct%(symbol)s').substitute(locals())

    format = [(f or tag.symbol) % tag, tag.ex_command]
    format[1] = format[1].strip()

    if show_path:
        format.insert(1, tag.filename)

    return format


def prepare_for_quickpanel(formatter=format_tag_for_quickopen, path_cols=()):
    """Prepare list of matching ctags for the quickpanel"""
    def compile_lists(sorter):
        args, display = [], []

        for t in sorter():
            display.append(formatter(t))
            args.append(t)

        return args, display  # format_for_display(display, paths=path_cols)

    return compile_lists


"""File collection helper functions"""


def commonfolder(m):
    if not m:
        return ''

    s1 = min(m).split(os.path.sep)
    s2 = max(m).split(os.path.sep)

    for i, c in enumerate(s1):
        if c != s2[i]:
            return os.path.sep.join(s1[:i])

    return os.path.sep.join(s1)


def files_to_search(file_name, tags_file, multiple=True):
    if multiple:
        return []

    tag_dir = os.path.normpath(os.path.dirname(tags_file))
    common_prefix = commonfolder([tag_dir, file_name])

    files = [file_name[len(common_prefix)+1:]]

    return files


def get_current_file_suffix(file_name):
    file_prefix, file_suffix = os.path.splitext(file_name)

    return file_suffix


"""
Sublime Commands
"""

"""Jumpback Commands"""


def different_mod_area(f1, f2, r1, r2):
    same_file = f1 == f2
    same_region = abs(r1[0] - r2[0]) < 40
    return not same_file or not same_region


class JumpBack(sublime_plugin.WindowCommand):
    def is_enabled(self, to=None):
        if to == 'last_modification':
            return len(self.mods) > 1
        return len(self.last) > 0

    def is_visible(self, to=None):
        return setting('show_context_menus')

    last = []
    mods = []

    def run(self, to=None):
        if to == 'last_modification' and self.mods:
            return self.lastModifications()

        if not JumpBack.last:
            return status_message('JumpBack buffer empty')

        f, sel = JumpBack.last.pop()
        self.jump(f, eval(sel))

    def lastModifications(self):
        # c)urrent v)iew, r)egion and f)ile
        cv = sublime.active_window().active_view()
        cr = eval(repr(cv.sel()[0]))
        cf = cv.file_name()

        # very latest, s)tarting modification
        sf, sr = JumpBack.mods.pop(0)

        if sf is None:
            return
        sr = eval(sr)

        in_different_mod_area = different_mod_area(sf, cf, cr, sr)

        # default j)ump f)ile and r)egion
        jf, jr = sf, sr

        if JumpBack.mods:
            for i, (f, r) in enumerate(JumpBack.mods):
                region = eval(r)
                if different_mod_area(sf, f, sr, region):
                    break

            del JumpBack.mods[:i]
            if not in_different_mod_area:
                jf, jr = f, region

        if in_different_mod_area or not JumpBack.mods:
            JumpBack.mods.insert(0, (jf, repr(jr)))

        self.jump(jf, jr)

    def jump(self, fn, sel):
        @on_load(fn, begin_edit=True)
        def and_then(view):
            select(view, sublime.Region(*sel))

    @classmethod
    def append(cls, view):
        fn = view.file_name()
        if fn:
            cls.last.append((fn, repr(view.sel()[0])))


class JumpBackListener(sublime_plugin.EventListener):
    def on_modified(self, view):
        sel = view.sel()
        if len(sel):
            JumpBack.mods.insert(0, (view.file_name(), repr(sel[0])))
            del JumpBack.mods[100:]


"""CTags commands"""


def show_tag_panel(view, result, jump_directly):
    if result not in (True, False, None):
        args, display = result
        if not args:
            return

        def on_select(i):
            if i != -1:
                JumpBack.append(view)
                scroll_to_tag(view, args[i])

        if jump_directly and len(args) == 1:
            on_select(0)
        else:
            view.window().show_quick_panel(display, on_select)


def ctags_goto_command(jump_directly=False):
    def wrapper(f):
        def command(self, edit, **args):
            view = self.view
            tags_file = find_tags_relative_to(
                view.file_name(), setting('tag_file'))

            if not tags_file:
                status_message('Can\'t find any relevant tags file')
                return

            result = f(self, self.view, args, tags_file)
            show_tag_panel(self.view, result, jump_directly)

        return command
    return wrapper


def check_if_building(self, **args):
    if RebuildTags.build_ctags.func.running:
        status_message('Please wait while tags are built')
    else:
        return True


def compile_filters(view):
    filters = []
    for selector, regexes in list(setting('filters', {}).items()):
        if view.match_selector(view.sel() and view.sel()[0].begin() or 0,
                               selector):
            filters.append(regexes)
    return filters


def compile_definition_filters(view):
    filters = []
    for selector, regexes in list(setting('definition_filters', {}).items()):
        if view.match_selector(view.sel() and view.sel()[0].begin() or 0,
                               selector):
            filters.append(regexes)
    return filters


"""Goto definition under cursor commands"""


class JumpToDefinition:
    @staticmethod
    def run(symbol, view, tags_file):
        tags = {}
        for tags_file in alternate_tags_paths(view, tags_file):
            tags = (TagFile(tags_file, SYMBOL)
                    .get_tags_dict(symbol, filters=compile_filters(view)))
            if tags:
                break

        if not tags:
            return status_message('Can\'t find "%s"' % symbol)

        def_filters = compile_definition_filters(view)

        def pass_def_filter(o):
            for f in def_filters:
                for k, v in list(f.items()):
                    if k in o:
                        if re.match(v, o[k]):
                            return False
            return True

        @prepare_for_quickpanel()
        def sorted_tags():
            p_tags = list(filter(pass_def_filter, tags.get(symbol, [])))
            if not p_tags:
                status_message('Can\'t find "%s"' % symbol)
            p_tags = sorted(p_tags, key=iget('tag_path'))
            return p_tags

        return sorted_tags


class NavigateToDefinition(sublime_plugin.TextCommand):
    is_enabled = check_if_building

    def __init__(self, args):
        sublime_plugin.TextCommand.__init__(self, args)
        self.scopes = re.compile(RUBY_SCOPES)
        self.endings = re.compile(RUBY_SPECIAL_ENDINGS)

    def is_visible(self):
        return setting('show_context_menus')

    @ctags_goto_command(jump_directly=True)
    def run(self, view, args, tags_file):
        region = view.sel()[0]
        if region.begin() == region.end():  # point
            region = view.word(region)
        symbol = view.substr(region)

        return JumpToDefinition.run(symbol, view, tags_file)


class SearchForDefinition(sublime_plugin.WindowCommand):
    is_enabled = check_if_building

    def is_visible(self):
        return setting('show_context_menus')

    def run(self):
        self.window.show_input_panel(
            '', '', self.on_done, self.on_change, self.on_cancel)

    def on_done(self, symbol):
        view = self.window.active_view()
        tags_file = find_tags_relative_to(
            view.file_name(), setting('tag_file'))

        if not tags_file:
            status_message('Can\'t find any relevant tags file')
            return

        result = JumpToDefinition.run(symbol, view, tags_file)
        show_tag_panel(view, result, True)

    def on_change(self, text):
        pass

    def on_cancel(self):
        pass


"""Show Symbol commands"""

tags_cache = defaultdict(dict)


class ShowSymbols(sublime_plugin.TextCommand):
    is_enabled = check_if_building

    def is_visible(self):
        return setting('show_context_menus')

    @ctags_goto_command()
    def run(self, view, args, tags_file):
        if not tags_file:
            return
        multi = args.get('type') == 'multi'
        lang = args.get('type') == 'lang'

        if view.file_name():
            files = files_to_search(view.file_name(), tags_file, multi)

        if lang:
            suffix = get_current_file_suffix(view.file_name())
            key = suffix
        else:
            key = ','.join(files)

        tags_file = tags_file + '_sorted_by_file'

        base_path = find_top_folder(view.window().folders(), view.file_name())

        def get_tags():
            loaded = TagFile(tags_file, FILENAME)
            if lang:
                return loaded.get_tags_dict_by_suffix(
                    suffix, filters=compile_filters(view))
            else:
                return loaded.get_tags_dict(
                    *files, filters=compile_filters(view))

        if key in tags_cache[base_path]:
            print('loading symbols from cache')
            tags = tags_cache[base_path][key]
        else:
            print('loading symbols from file')
            tags = get_tags()
            tags_cache[base_path][key] = tags

        print(('loaded [%d] symbols' % len(tags)))

        if not tags:
            if multi:
                view.run_command('show_symbols', {'type': 'multi'})
            else:
                sublime.status_message(
                    'No symbols found **FOR CURRENT FILE**; Try Rebuild?')

        path_cols = (0, ) if len(files) > 1 or multi else ()
        formatting = functools.partial(format_tag_for_quickopen,
                                       show_path=bool(path_cols))

        @prepare_for_quickpanel(formatting, path_cols=())
        def sorted_tags():
            return sorted(
                chain(*(tags[k] for k in tags)), key=iget('tag_path'))

        return sorted_tags


"""Rebuild CTags commands"""


class RebuildTags(sublime_plugin.TextCommand):
    """Handler for the 'rebuild_tags' command.

    Command (re)builds tag files for the open file or folder(s), reading
    relevant settings from the settings file.
    """
    def run(self, edit, **args):
        """Handler for rebuild_tags command"""
        paths = []

        # user has requested to rebuild tags for the specific folders (via
        # context menu in Folders pane)
        if 'dirs' in args:
            paths.extend(args['dirs'])
        # file open, rebuild tags relative to the file
        elif self.view.file_name() is not None:
            # Rebuild and rebuild tags relative to the currently opened file
            paths.append(self.view.file_name())
        # no file is open, build tags for all opened folders
        elif len(self.view.window().folders()) > 0:
            # No file is open, rebuild tags for all opened folders
            paths.extend(self.view.window().folders())
        # no file or folder open, return
        else:
            status_message('Cannot build CTags: No file or folder open.')
            return

        command = setting('command', setting('ctags_command'))
        recursive = setting('recursive')
        tag_file = setting('tag_file')

        self.build_ctags(paths, tag_file, recursive, command)

    @threaded(msg='Already running CTags!')
    def build_ctags(self, paths, tag_file, recursive, command):
        """Build tags for the open file or folder(s)"""

        def tags_building(tag_file):
            """Display 'Building CTags' message in all views"""
            print(('Building CTags for %s: Please be patient' % tag_file))
            in_main(lambda: status_message('Building CTags for {0}: Please be'
                                           ' patient'.format(tag_file)))()

        def tags_built(tag_file):
            """Display 'Finished Building CTags' message in all views"""
            print(('Finished building %s' % tag_file))
            in_main(lambda: status_message('Finished building {0}'
                                           .format(tag_file)))()
            in_main(lambda: tags_cache[os.path.dirname(tag_file)].clear())()

        for path in paths:
            tags_building(path)
            try:
                result = ctags.build_ctags(path=path, tag_file=tag_file,
                                           recursive=recursive, cmd=command)
            except EnvironmentError as e:
                str_err = ' '.join(e.strerror.decode('utf-8').splitlines())
                error_message(str_err)  # show error message
                return
            except IOError as e:
                error_message(str(e).rstrip())
                return
            tags_built(result)

        GetAllCTagsList.ctags_list = []  # clear the cached ctags list


"""Autocomplete commands"""


class GetAllCTagsList():
    ctags_list = []

    """cache all the ctags list"""
    def __init__(self, list):
        self.ctags_list = list


class CTagsAutoComplete(sublime_plugin.EventListener):
    def on_query_completions(self, view, prefix, locations):
        if setting('autocomplete'):
            prefix = prefix.strip().lower()
            tags_path = view.window().folders()[0] + '/' + setting('tag_file')

            sub_results = [v.extract_completions(prefix)
                           for v in sublime.active_window().views()]
            sub_results = [(item, item) for sublist in sub_results
                           for item in sublist]  # flatten

            if GetAllCTagsList.ctags_list:
                results = [sublist for sublist in GetAllCTagsList.ctags_list
                           if sublist[0].lower().startswith(prefix)]
                results = sorted(set(results).union(set(sub_results)))

                return results
            else:
                tags = []

                # check if a project is open and the tags file exists
                if not (view.window().folders() and os.path.exists(tags_path)):
                    return tags

                f = os.popen("awk '{ print $1 }' '" + tags_path + "'")

                for i in f.readlines():
                    tags.append([i.strip()])

                tags = [(item, item) for sublist in tags
                        for item in sublist]  # flatten
                tags = sorted(set(tags))  # make unique
                GetAllCTagsList.ctags_list = tags
                results = [sublist for sublist in GetAllCTagsList.ctags_list
                           if sublist[0].lower().startswith(prefix)]
                results = list(set(results).union(set(sub_results)))
                results.sort()

                return results


"""Test CTags commands"""


class TestCtags(sublime_plugin.TextCommand):
    routine = None

    def run(self, edit, **args):
        if self.routine is None:
            self.routine = self.co_routine(self.view)
            next(self.routine)

    def __next__(self):
        try:
            next(self.routine)
        except Exception as e:
            print(e)
            self.routine = None

    def co_routine(self, view):
        tag_file = find_tags_relative_to(
            view.file_name(), setting('tag_file'))

        with codecs.open(tag_file, encoding='utf-8') as tf:
            tags = parse_tag_lines(tf, tag_class=Tag)

        print('Starting Test')

        ex_failures = []
        line_failures = []

        for symbol, tag_list in list(tags.items()):
            for tag in tag_list:
                tag.root_dir = os.path.dirname(tag_file)

                def hook(av):
                    test_context = av.sel()[0]

                    if tag.ex_command.isdigit():
                        test_string = tag.symbol
                    else:
                        test_string = tag.ex_command
                        test_context = av.line(test_context)

                    if not av.substr(test_context).startswith(test_string):
                        failure = 'FAILURE %s' % pprint.pformat(tag)
                        failure += av.file_name()

                        if setting('debug'):
                            if not sublime.question_box('%s\n\n\n' % failure):
                                self.routine = None

                            return sublime.set_clipboard(failure)
                        ex_failures.append(tag)
                    sublime.set_timeout(self.__next__, 5)
                scroll_to_tag(view, tag, hook)
                yield

        failures = line_failures + ex_failures
        tags_tested = sum(len(v) for v in list(tags.values())) - len(failures)

        view = sublime.active_window().new_file()

        with Edit(view) as edit:
            edit.insert(view.size(), '%s Tags Tested OK\n' % tags_tested)
            edit.insert(view.size(), '%s Tags Failed' % len(failures))

        view.set_scratch(True)
        view.set_name('CTags Test Results')

        if failures:
            sublime.set_clipboard(pprint.pformat(failures))
