# (c) 2012, Michael DeHaan <michael.dehaan@gmail.com>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

import sys
import os
import shlex
import re
import codecs
import jinja2
import yaml
import operator
import json
from ansible import errors
from ansible import __version__
import ansible.constants as C
import time
import StringIO
import imp
import glob
import subprocess
import datetime
import pwd

# TODO: refactor this file

_LISTRE = re.compile(r"(\w+)\[(\d+)\]")


def _varFindLimitSpace(space, part, depth):

    # TODO: comments

    if space is None:
        return space
    if part[0] == '{' and part[-1] == '}':
        part = part[1:-1]
    part = varReplace(part, vars, depth=depth + 1)
    if part in space:
        space = space[part]
    elif "[" in part:
        m = _LISTRE.search(part)
        if not m:
            return None
        else:
            try:
                space = space[m.group(1)][int(m.group(2))]
            except (KeyError, IndexError):
                return None
    else:
        return None
    return space

def _varFind(text, vars, depth=0):

    # TODO: comments

    start = text.find("$")
    if start == -1:
        return None
    # $ as last character
    if start + 1 == len(text):
        return None
    # Escaped var
    if start > 0 and text[start - 1] == '\\':
        return {'replacement': '$', 'start': start - 1, 'end': start + 1}

    var_start = start + 1
    if text[var_start] == '{':
        is_complex = True
        brace_level = 1
        var_start += 1
    else:
        is_complex = False
        brace_level = 0
    end = var_start
    path = []
    part_start = (var_start, brace_level)
    space = vars
    while end < len(text) and ((is_complex and brace_level > 0) or not is_complex):
        if text[end].isalnum() or text[end] == '_':
            pass
        elif is_complex and text[end] == '{':
            brace_level += 1
        elif is_complex and text[end] == '}':
            brace_level -= 1
        elif is_complex and text[end] in ('$', '[', ']'):
            pass
        elif is_complex and text[end] == '.':
            if brace_level == part_start[1]:
                space = _varFindLimitSpace(space, text[part_start[0]:end], depth)
                part_start = (end + 1, brace_level)
        else:
            break
        end += 1
    var_end = end
    if is_complex:
        var_end -= 1
        if text[var_end] != '}' or brace_level != 0:
            return None
    if var_end == part_start[0]:
        return None
    space = _varFindLimitSpace(space, text[part_start[0]:var_end], depth)
    return {'replacement': space, 'start': start, 'end': end}

def varReplace(raw, vars, depth=0, expand_lists=False):
    ''' Perform variable replacement of $variables in string raw using vars dictionary '''
    # this code originally from yum

    if (depth > 20):
        raise errors.AnsibleError("template recursion depth exceeded")

    done = [] # Completed chunks to return

    while raw:
        m = _varFind(raw, vars, depth)
        if not m:
            done.append(raw)
            break

        # Determine replacement value (if unknown variable then preserve
        # original)

        replacement = m['replacement']
        if expand_lists and isinstance(replacement, (list, tuple)):
            replacement = ",".join(replacement)
        if isinstance(replacement, (str, unicode)):
            replacement = varReplace(replacement, vars, depth=depth+1, expand_lists=expand_lists)
        if replacement is None:
            replacement = raw[m['start']:m['end']]

        start, end = m['start'], m['end']
        done.append(raw[:start])          # Keep stuff leading up to token
        done.append(unicode(replacement)) # Append replacement value
        raw = raw[end:]                   # Continue with remainder of string

    return ''.join(done)

_FILEPIPECRE = re.compile(r"\$(?P<special>FILE|PIPE)\(([^\)]+)\)")
def _varReplaceFilesAndPipes(basedir, raw):
    done = [] # Completed chunks to return

    while raw:
        m = _FILEPIPECRE.search(raw)
        if not m:
            done.append(raw)
            break

        # Determine replacement value (if unknown variable then preserve
        # original)

        replacement = m.group()
        if m.group(1) == "FILE":
            from ansible import utils
            path = utils.path_dwim(basedir, m.group(2))
            try:
                f = open(path, "r")
                replacement = f.read()
                f.close()
            except IOError:
                raise errors.AnsibleError("$FILE(%s) failed" % path)
        elif m.group(1) == "PIPE":
            p = subprocess.Popen(m.group(2), shell=True, stdout=subprocess.PIPE)
            (stdout, stderr) = p.communicate()
            if p.returncode == 0:
                replacement = stdout
            else:
                raise errors.AnsibleError("$PIPE(%s) returned %d" % (m.group(2), p.returncode))

        start, end = m.span()
        done.append(raw[:start])    # Keep stuff leading up to token
        done.append(replacement.rstrip())    # Append replacement value
        raw = raw[end:]             # Continue with remainder of string

    return ''.join(done)

def varReplaceWithItems(basedir, varname, vars):
    ''' helper function used by with_items '''

    if isinstance(varname, basestring):
        m = _varFind(varname, vars)
        if not m:
            return varname
        if m['start'] == 0 and m['end'] == len(varname):
            try:
                return varReplaceWithItems(basedir, m['replacement'], vars)
            except VarNotFoundException:
                return varname
        else:
            return template(basedir, varname, vars)
    elif isinstance(varname, (list, tuple)):
        return [varReplaceWithItems(basedir, v, vars) for v in varname]
    elif isinstance(varname, dict):
        d = {}
        for (k, v) in varname.iteritems():
            d[k] = varReplaceWithItems(basedir, v, vars)
        return d
    else:
        return varname

def template(basedir, text, vars, expand_lists=False):
    ''' run a text buffer through the templating engine until it no longer changes '''

    prev_text = ''
    try:
        text = text.decode('utf-8')
    except UnicodeEncodeError:
        pass # already unicode
    text = varReplace(unicode(text), vars, expand_lists=expand_lists)
    text = _varReplaceFilesAndPipes(basedir, text)
    return text

def template_from_file(basedir, path, vars):
    ''' run a file through the templating engine '''

    from ansible import utils
    realpath = utils.path_dwim(basedir, path)
    loader=jinja2.FileSystemLoader([basedir,os.path.dirname(realpath)])
    environment = jinja2.Environment(loader=loader, trim_blocks=True)
    environment.filters['to_json'] = json.dumps
    environment.filters['from_json'] = json.loads
    environment.filters['to_yaml'] = yaml.dump
    environment.filters['from_yaml'] = yaml.load

    ### Load filter plugins
    filter_plugin_list = utils.import_plugins(os.path.join(basedir,'filter_plugins'))
    for i in reversed(C.DEFAULT_FILTER_PLUGIN_PATH.split(os.pathsep)):
        filter_plugin_list.update(utils.import_plugins(i))
    for k,v in filter_plugin_list.items():
        o = v.FilterModule()
        for name,filter in o.filters().items():
            environment.filters[name] = filter

    try:
        data = codecs.open(realpath, encoding="utf8").read()
    except UnicodeDecodeError:
        raise errors.AnsibleError("unable to process as utf-8: %s" % realpath)
    except:
        raise errors.AnsibleError("unable to read %s" % realpath)
    t = environment.from_string(data)
    vars = vars.copy()
    try:
        template_uid = pwd.getpwuid(os.stat(realpath).st_uid).pw_name
    except:
        template_uid = os.stat(realpath).st_uid
    vars['template_host']   = os.uname()[1]
    vars['template_path']   = realpath
    vars['template_mtime']  = datetime.datetime.fromtimestamp(os.path.getmtime(realpath))
    vars['template_uid']    = template_uid

    managed_default = C.DEFAULT_MANAGED_STR
    managed_str = managed_default.format(
                    host = vars['template_host'],
                    uid  = vars['template_uid'],
                    file = vars['template_path']
                    )
    vars['ansible_managed'] = time.strftime(managed_str,
                                time.localtime(os.path.getmtime(realpath)))

    res = t.render(vars)
    if data.endswith('\n') and not res.endswith('\n'):
        res = res + '\n'
    return template(basedir, res, vars)

