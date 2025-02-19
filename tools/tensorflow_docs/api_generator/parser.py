# Lint as: python3
# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Turn Python docstrings into Markdown for TensorFlow documentation."""

import dataclasses
import enum
import inspect
import os
import posixpath
import pprint
import re
import textwrap
import typing

from typing import Any, Dict, List, Tuple, Iterable, Optional, Union

from tensorflow_docs.api_generator import config
from tensorflow_docs.api_generator import doc_generator_visitor
from tensorflow_docs.api_generator import obj_type as obj_type_lib


@dataclasses.dataclass
class FileLocation(object):
  """This class indicates that the object is defined in a regular file.

  This can be used for the `defined_in` slot of the `PageInfo` objects.
  """

  base_url: Optional[str] = None
  start_line: Optional[int] = None
  end_line: Optional[int] = None

  @property
  def url(self) -> Optional[str]:
    if self.start_line and self.end_line:
      if 'github.com' in self.base_url:
        return f'{self.base_url}#L{self.start_line}-L{self.end_line}'
    return self.base_url


def is_class_attr(full_name, index):
  """Check if the object's parent is a class.

  Args:
    full_name: The full name of the object, like `tf.module.symbol`.
    index: The {full_name:py_object} dictionary for the public API.

  Returns:
    True if the object is a class attribute.
  """
  parent_name = full_name.rsplit('.', 1)[0]
  if inspect.isclass(index[parent_name]):
    return True

  return False


def documentation_path(full_name, is_fragment=False):
  """Returns the file path for the documentation for the given API symbol.

  Given the fully qualified name of a library symbol, compute the path to which
  to write the documentation for that symbol (relative to a base directory).
  Documentation files are organized into directories that mirror the python
  module/class structure.

  Args:
    full_name: Fully qualified name of a library symbol.
    is_fragment: If `False` produce a page link (`tf.a.b.c` -->
      `tf/a/b/c.md`). If `True` produce fragment link, `tf.a.b.c` -->
      `tf/a/b.md#c`

  Returns:
    The file path to which to write the documentation for `full_name`.
  """
  parts = full_name.split('.')
  if is_fragment:
    parts, fragment = parts[:-1], parts[-1]

  result = posixpath.join(*parts) + '.md'

  if is_fragment:
    result = result + '#' + fragment

  return result


def _get_raw_docstring(py_object):
  """Get the docs for a given python object.

  Args:
    py_object: A python object to retrieve the docs for (class, function/method,
      or module).

  Returns:
    The docstring, or the empty string if no docstring was found.
  """

  if obj_type_lib.ObjType.get(py_object) is obj_type_lib.ObjType.TYPE_ALIAS:
    if inspect.getdoc(py_object) != inspect.getdoc(py_object.__origin__):
      result = inspect.getdoc(py_object)
    else:
      result = ''
  elif obj_type_lib.ObjType.get(py_object) is not obj_type_lib.ObjType.OTHER:
    result = inspect.getdoc(py_object) or ''
  else:
    result = ''

  if result is None:
    result = ''

  result = _StripTODOs()(result)
  result = _StripPylintAndPyformat()(result)
  result = _AddDoctestFences()(result + '\n')
  result = _DowngradeH1Keywords()(result)
  return result


class _AddDoctestFences(object):
  """Adds ``` fences around doctest caret blocks >>> that don't have them."""
  CARET_BLOCK_RE = re.compile(
      r"""
    \n                                     # After a blank line.
    (?P<indent>\ *)(?P<content>\>\>\>.*?)  # Whitespace and a triple caret.
    \n\s*?(?=\n|$)                         # Followed by a blank line""",
      re.VERBOSE | re.DOTALL)

  def _sub(self, match):
    groups = match.groupdict()
    fence = f"\n{groups['indent']}```\n"

    content = groups['indent'] + groups['content']
    return ''.join([fence, content, fence])

  def __call__(self, content):
    return self.CARET_BLOCK_RE.sub(self._sub, content)


class _StripTODOs(object):
  TODO_RE = re.compile('#? *TODO.*')

  def __call__(self, content: str) -> str:
    return self.TODO_RE.sub('', content)


class _StripPylintAndPyformat(object):
  STRIP_RE = re.compile('# *?(pylint|pyformat):.*', re.I)

  def __call__(self, content: str) -> str:
    return self.STRIP_RE.sub('', content)


class _DowngradeH1Keywords():
  """Convert keras docstring keyword format to google format."""

  KEYWORD_H1_RE = re.compile(
      r"""
    ^                 # Start of line
    (?P<indent>\s*)   # Capture leading whitespace as <indent
    \#\s*             # A literal "#" and more spaces
                      # Capture any of these keywords as <keyword>
    (?P<keyword>Args|Arguments|Returns|Raises|Yields|Examples?|Notes?)
    \s*:?             # Optional whitespace and optional ":"
    """, re.VERBOSE)

  def __call__(self, docstring):
    lines = docstring.splitlines()

    new_lines = []
    is_code = False
    for line in lines:
      if line.strip().startswith('```'):
        is_code = not is_code
      elif not is_code:
        line = self.KEYWORD_H1_RE.sub(r'\g<indent>\g<keyword>:', line)
      new_lines.append(line)

    docstring = '\n'.join(new_lines)
    return docstring


def _handle_compatibility(doc) -> Tuple[str, Dict[str, str]]:
  """Parse and remove compatibility blocks from the main docstring.

  Args:
    doc: The docstring that contains compatibility notes.

  Returns:
    A tuple of the modified doc string and a hash that maps from compatibility
    note type to the text of the note.
  """
  compatibility_notes = {}
  match_compatibility = re.compile(
      r'[ \t]*@compatibility\(([^\n]+?)\)\s*\n'
      r'(.*?)'
      r'[ \t]*@end_compatibility', re.DOTALL)
  for f in match_compatibility.finditer(doc):
    compatibility_notes[f.group(1)] = f.group(2)
  return match_compatibility.subn(r'', doc)[0], compatibility_notes


def _pairs(items):
  """Given an list of items [a,b,a,b...], generate pairs [(a,b),(a,b)...].

  Args:
    items: A list of items (length must be even)

  Returns:
    A list of pairs.
  """
  assert len(items) % 2 == 0
  return list(zip(items[::2], items[1::2]))


# Don't change the width="214px" without consulting with the devsite-team.
TABLE_TEMPLATE = textwrap.dedent("""
  <!-- Tabular view -->
   <table class="responsive fixed orange">
  <colgroup><col width="214px"><col></colgroup>
  <tr><th colspan="2">{title}</th></tr>
  {text}
  {items}
  </table>
  """)

ITEMS_TEMPLATE = textwrap.dedent("""\
  <tr>
  <td>
  {name}{anchor}
  </td>
  <td>
  {description}
  </td>
  </tr>""")

TEXT_TEMPLATE = textwrap.dedent("""\
  <tr class="alt">
  <td colspan="2">
  {text}
  </td>
  </tr>""")


@dataclasses.dataclass
class TitleBlock(object):
  """A class to parse title blocks (like `Args:`) and convert them to markdown.

  This handles the "Args/Returns/Raises" blocks and anything similar.

  These are used to extract metadata (argument descriptions, etc), and upgrade
  This `TitleBlock` to markdown.

  These blocks are delimited by indentation. There must be a blank line before
  the first `TitleBlock` in a series.

  The expected format is:

  ```
  Title:
    Freeform text
    arg1: value1
    arg2: value1
  ```

  These are represented as:

  ```
  TitleBlock(
    title = "Arguments",
    text = "Freeform text",
    items=[('arg1', 'value1'), ('arg2', 'value2')])
  ```

  The "text" and "items" fields may be empty. When both are empty the generated
  markdown only serves to upgrade the title to a <h4>.

  Attributes:
    title: The title line, without the colon.
    text: Freeform text. Anything between the `title`, and the `items`.
    items: A list of (name, value) string pairs. All items must have the same
      indentation.
  """

  _INDENTATION_REMOVAL_RE = re.compile(r'( *)(.+)')

  title: Optional[str]
  text: str
  items: Iterable[Tuple[str, str]]

  def _dedent_after_first_line(self, text):
    if '\n' not in text:
      return text

    first, remainder = text.split('\n', 1)
    remainder = textwrap.dedent(remainder)
    result = '\n'.join([first, remainder])
    return result

  def table_view(self, title_template: Optional[str] = None) -> str:
    """Returns a tabular markdown version of the TitleBlock.

    Tabular view is only for `Args`, `Returns`, `Raises` and `Attributes`. If
    anything else is encountered, redirect to list view.

    Args:
      title_template: Template for title detailing how to display it.

    Returns:
      Table containing the content to display.
    """

    if title_template is not None:
      title = title_template.format(title=self.title)
    else:
      title = self.title

    text = self.text.strip()
    if text:
      text = self._dedent_after_first_line(text)
      text = TEXT_TEMPLATE.format(text=text)

    items = []
    for name, description in self.items:
      if not description:
        description = ''
      else:
        description = description.strip('\n')
        description = self._dedent_after_first_line(description)
      item_table = ITEMS_TEMPLATE.format(
          name=f'`{name}`', anchor='', description=description)
      items.append(item_table)

    return '\n' + TABLE_TEMPLATE.format(
        title=title, text=text, items=''.join(items)) + '\n'

  def __str__(self) -> str:
    """Returns a non-tempated version of the TitleBlock."""

    sub = []
    sub.append(f'\n\n#### {self.title}:\n')
    sub.append(textwrap.dedent(self.text))
    sub.append('\n')

    for name, description in self.items:
      description = description.strip()
      if not description:
        sub.append(f'* <b>`{name}`</b>\n')
      else:
        sub.append(f'* <b>`{name}`</b>: {description}\n')

    return ''.join(sub)

  # This regex matches an entire title-block.
  BLOCK_RE = re.compile(
      r"""
      (?:^|^\n|\n\n)                  # After a blank line (non-capturing):
        (?P<title>[A-Z][\s\w]{0,20})  # Find a sentence case title, followed by
          \s*:\s*?(?=\n)              # whitespace, a colon and a new line.
      (?P<content>.*?)                # Then take everything until
        (?=\n\S|$)                    # look ahead finds a non-indented line
                                      # (a new-line followed by non-whitespace)
    """, re.VERBOSE | re.DOTALL)

  ITEM_RE = re.compile(
      r"""
      ^(\*?\*?'?"?     # Capture optional *s to allow *args, **kwargs and quotes
          \w[\w.'"]*?  # Capture a word character followed by word characters
                       # or "."s or ending quotes.
      )\s*:\s          # Allow any whitespace around the colon.""",
      re.MULTILINE | re.VERBOSE)

  @classmethod
  def split_string(cls, docstring: str):
    r"""Given a docstring split it into a list of `str` or `TitleBlock` chunks.

    For example the docstring of `tf.nn.relu`:

    '''
    Computes `max(features, 0)`.

    Args:
      features: A `Tensor`. Must be one of the following types: `float32`,
        `float64`, `int32`, `int64`, `uint8`, `int16`, `int8`, `uint16`, `half`.
      name: A name for the operation (optional).

    More freeform markdown text.
    '''

    This is parsed, and returned as:

    ```
    [
        "Computes rectified linear: `max(features, 0)`.",
        TitleBlock(
          title='Args',
          text='',
          items=[
            ('features', ' A `Tensor`. Must be...'),
            ('name', ' A name for the operation (optional).\n\n')]
        ),
        "More freeform markdown text."
    ]
    ```
    Args:
      docstring: The docstring to parse

    Returns:
      The docstring split into chunks. Each chunk produces valid markdown when
      `str` is called on it (each chunk is a python `str`, or a `TitleBlock`).
    """
    parts = []
    while docstring:
      split = re.split(cls.BLOCK_RE, docstring, maxsplit=1)
      # The first chunk in split is all text before the TitleBlock.
      before = split.pop(0)
      parts.append(before)

      # If `split` is empty, there were no other matches, and we're done.
      if not split:
        break

      # If there was a match,  split contains three items. The two capturing
      # groups in the RE, and the remainder.
      title, content, docstring = split

      # Now `content` contains the text and the name-value item pairs.
      # separate these two parts.
      content = textwrap.dedent(content)
      split = cls.ITEM_RE.split(content)
      text = split.pop(0)
      items = _pairs(split)

      title_block = cls(title=title, text=text, items=items)
      parts.append(title_block)

    return parts


class DocstringInfo(typing.NamedTuple):
  brief: str
  docstring_parts: List[Union[TitleBlock, str]]
  compatibility: Dict[str, str]


def _get_other_member_doc(
    obj: Any,
    parser_config: config.ParserConfig,
    extra_docs: Optional[Dict[int, str]],
) -> str:
  """Returns the docs for other members of a module."""

  # An object's __doc__ attribute will mask the class'.
  my_doc = inspect.getdoc(obj)
  class_doc = inspect.getdoc(type(obj))

  description = None
  if my_doc != class_doc:
    # If they're different it's because __doc__ is set on the instance.
    if my_doc is not None:
      description = my_doc

  if description is None and extra_docs is not None:
    description = extra_docs.get(id(obj), None)

  info = None
  if isinstance(obj, dict):
    # pprint.pformat (next block) doesn't sort dicts until python 3.8
    items = [
        f' {name!r}: {value!r}'
        for name, value in sorted(obj.items(), key=repr)
    ]
    items = ',\n'.join(items)
    info = f'```\n{{\n{items}\n}}\n```'

  elif isinstance(obj, (set, frozenset)):
    # pprint.pformat (next block) doesn't sort dicts until python 3.8
    items = [f' {value!r}' for value in sorted(obj, key=repr)]
    items = ',\n'.join(items)
    info = f'```\n{{\n{items}\n}}\n```'
  elif (doc_generator_visitor.maybe_singleton(obj) or
        isinstance(obj, (list, tuple, enum.Enum))):
    # * Use pformat instead of repr so dicts and sets are sorted (deterministic)
    # * Escape ` so it doesn't break code formatting. You can't use "&#96;"
    #   here since it will diaplay as a literal. I'm not sure why <pre></pre>
    #   breaks on the site.
    info = pprint.pformat(obj).replace('`', r'\`')
    info = f'`{info}`'
  elif obj_type_lib.ObjType.get(obj) is obj_type_lib.ObjType.PROPERTY:
    info = None
  else:
    class_full_name = parser_config.reverse_index.get(id(type(obj)), None)
    if class_full_name is None:
      module = getattr(type(obj), '__module__', None)
      class_name = type(obj).__name__
      if module is None or module == 'builtins':
        class_full_name = class_name
      else:
        class_full_name = f'{module}.{class_name}'
    info = f'Instance of `{class_full_name}`'

  parts = [info, description]
  parts = [item for item in parts if item is not None]

  return '\n\n'.join(parts)


def parse_md_docstring(
    py_object: Any,
    full_name: str,
    parser_config: config.ParserConfig,
    extra_docs: Optional[Dict[int, str]] = None,
) -> DocstringInfo:
  """Parse the object's docstring and return a `DocstringInfo`.

  This function clears @@'s from the docstring, and replaces `` references
  with links.

  Args:
    py_object: A python object to retrieve the docs for (class, function/method,
      or module).
    full_name: (optional) The api path to the current object, so replacements
      can depend on context.
    parser_config: An instance of `config.ParserConfig`.
    extra_docs: Extra docs for symbols like public constants(list, tuple, etc)
      that need to be added to the markdown pages created.

  Returns:
    A DocstringInfo object, all fields will be empty if no docstring was found.
  """

  if obj_type_lib.ObjType.get(py_object) is obj_type_lib.ObjType.OTHER:
    raw_docstring = _get_other_member_doc(
        obj=py_object, parser_config=parser_config, extra_docs=extra_docs)
  else:
    raw_docstring = _get_raw_docstring(py_object)

  raw_docstring = parser_config.reference_resolver.replace_references(
      raw_docstring, full_name)

  atat_re = re.compile(r' *@@[a-zA-Z_.0-9]+ *$')
  raw_docstring = '\n'.join(
      line for line in raw_docstring.split('\n') if not atat_re.match(line))

  docstring, compatibility = _handle_compatibility(raw_docstring)

  if 'Generated by: tensorflow/tools/api/generator' in docstring:
    docstring = ''

  # Remove the first-line "brief" docstring.
  lines = docstring.split('\n')
  brief = lines.pop(0)

  docstring = '\n'.join(lines)

  docstring_parts = TitleBlock.split_string(docstring)

  return DocstringInfo(brief, docstring_parts, compatibility)


def get_defining_class(py_class, name):
  for cls in inspect.getmro(py_class):
    if name in cls.__dict__:
      return cls
  return None


def _unwrap_obj(obj):
  while True:
    unwrapped_obj = getattr(obj, '__wrapped__', None)
    if unwrapped_obj is None:
      break
    obj = unwrapped_obj
  return obj


def get_defined_in(
    py_object: Any,
    parser_config: config.ParserConfig) -> Optional[FileLocation]:
  """Returns a description of where the passed in python object was defined.

  Args:
    py_object: The Python object.
    parser_config: A config.ParserConfig object.

  Returns:
    A `FileLocation`
  """
  # Every page gets a note about where this object is defined
  base_dirs_and_prefixes = zip(parser_config.base_dir,
                               parser_config.code_url_prefix)
  try:
    obj_path = inspect.getfile(_unwrap_obj(py_object))
  except TypeError:  # getfile throws TypeError if py_object is a builtin.
    return None

  if not obj_path.endswith(('.py', '.pyc')):
    return None

  code_url_prefix = None
  for base_dir, temp_prefix in base_dirs_and_prefixes:
    rel_path = os.path.relpath(path=obj_path, start=base_dir)
    # A leading ".." indicates that the file is not inside `base_dir`, and
    # the search should continue.
    if rel_path.startswith('..'):
      continue
    else:
      code_url_prefix = temp_prefix
      # rel_path is currently a platform-specific path, so we need to convert
      # it to a posix path (for lack of a URL path).
      rel_path = posixpath.join(*rel_path.split(os.path.sep))
      break

  # No link if the file was not found in a `base_dir`, or the prefix is None.
  if code_url_prefix is None:
    return None

  try:
    lines, start_line = inspect.getsourcelines(py_object)
    end_line = start_line + len(lines) - 1
    if 'MACHINE GENERATED' in lines[0]:
      # don't link to files generated by tf_export
      return None
  except (IOError, TypeError, IndexError):
    start_line = None
    end_line = None

  # In case this is compiled, point to the original
  if rel_path.endswith('.pyc'):
    # If a PY3 __pycache__/ subdir is being used, omit it.
    rel_path = rel_path.replace('__pycache__' + os.sep, '')
    # Strip everything after the first . so that variants such as .pyc and
    # .cpython-3x.pyc or similar are all handled.
    rel_path = rel_path.partition('.')[0] + '.py'

  if re.search(r'<[\w\s]+>', rel_path):
    # Built-ins emit paths like <embedded stdlib>, <string>, etc.
    return None
  if '<attrs generated' in rel_path:
    return None

  if re.match(r'.*/gen_[^/]*\.py$', rel_path):
    return FileLocation()
  if 'genfiles' in rel_path:
    return FileLocation()
  elif re.match(r'.*_pb2\.py$', rel_path):
    # The _pb2.py files all appear right next to their defining .proto file.
    rel_path = rel_path[:-7] + '.proto'
    return FileLocation(base_url=posixpath.join(code_url_prefix, rel_path))
  else:
    return FileLocation(
        base_url=posixpath.join(code_url_prefix, rel_path),
        start_line=start_line,
        end_line=end_line)


# TODO(markdaoust): This should just parse, pretty_docs should generate the md.
def generate_global_index(library_name, index, reference_resolver):
  """Given a dict of full names to python objects, generate an index page.

  The index page generated contains a list of links for all symbols in `index`
  that have their own documentation page.

  Args:
    library_name: The name for the documented library to use in the title.
    index: A dict mapping full names to python objects.
    reference_resolver: An instance of ReferenceResolver.

  Returns:
    A string containing an index page as Markdown.
  """
  symbol_links = []
  for full_name, py_object in index.items():
    obj_type = obj_type_lib.ObjType.get(py_object)
    if obj_type in (obj_type_lib.ObjType.OTHER, obj_type_lib.ObjType.PROPERTY):
      continue
    # In Python 3, unbound methods are functions, so eliminate those.
    if obj_type is obj_type_lib.ObjType.CALLABLE:
      if is_class_attr(full_name, index):
        continue
    with reference_resolver.temp_prefix('..'):
      symbol_links.append(
          (full_name, reference_resolver.python_link(full_name, full_name)))

  lines = [f'# All symbols in {library_name}', '']
  lines.append('<!-- Insert buttons and diff -->\n')

  # Sort all the symbols once, so that the ordering is preserved when its broken
  # up into main symbols and compat symbols and sorting the sublists is not
  # required.
  symbol_links = sorted(symbol_links, key=lambda x: x[0])

  compat_v1_symbol_links = []
  compat_v2_symbol_links = []
  primary_symbol_links = []

  for symbol, link in symbol_links:
    if symbol.startswith('tf.compat.v1'):
      if 'raw_ops' not in symbol:
        compat_v1_symbol_links.append(link)
    elif symbol.startswith('tf.compat.v2'):
      compat_v2_symbol_links.append(link)
    else:
      primary_symbol_links.append(link)

  lines.append('## Primary symbols')
  for link in primary_symbol_links:
    lines.append(f'*  {link}')

  if compat_v2_symbol_links:
    lines.append('\n## Compat v2 symbols\n')
    for link in compat_v2_symbol_links:
      lines.append(f'*  {link}')

  if compat_v1_symbol_links:
    lines.append('\n## Compat v1 symbols\n')
    for link in compat_v1_symbol_links:
      lines.append(f'*  {link}')

  # TODO(markdaoust): use a _ModulePageInfo -> prety_docs.build_md_page()
  return '\n'.join(lines)


class Metadata(object):
  """A class for building a page's Metadata block.

  Attributes:
    name: The name of the page being described by the Metadata block.
    version: The source version.
  """

  def __init__(self, name, version=None, content=None):
    """Creates a Metadata builder.

    Args:
      name: The name of the page being described by the Metadata block.
      version: The source version.
      content: Content to create the metadata from.
    """

    self.name = name

    self.version = version
    if self.version is None:
      self.version = 'Stable'

    self._content = content
    if self._content is None:
      self._content = []

  def append(self, item):
    """Adds an item from the page to the Metadata block.

    Args:
      item: The parsed page section to add.
    """
    self._content.append(item.short_name)

  def build_html(self):
    """Returns the Metadata block as an Html string."""
    # Note: A schema is not a URL. It is defined with http: but doesn't resolve.
    schema = 'http://developers.google.com/ReferenceObject'
    parts = [f'<div itemscope itemtype="{schema}">']

    parts.append(f'<meta itemprop="name" content="{self.name}" />')
    parts.append(f'<meta itemprop="path" content="{self.version}" />')
    for item in self._content:
      parts.append(f'<meta itemprop="property" content="{item}"/>')

    parts.extend(['</div>', ''])

    return '\n'.join(parts)
