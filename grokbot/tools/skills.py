"""Skill tools (procedural memory) backed by the existing SkillStore."""
import json

from grokbot.tools import ToolError


def register(registry):
    @registry.tool('list_skills', 'List saved skills (reusable workflows). Optional keyword search and offset for paging.',
                   {'type': 'object', 'properties': {
                       'query': {'type': 'string', 'description': 'Keyword search over name/description'},
                       'offset': {'type': 'integer'}},
                    'required': []}, category='skills')
    def list_skills(ctx, query='', offset=0):
        try:
            return json.dumps(ctx.workspace.skills.list_page(query or '', max(0, int(offset or 0))), ensure_ascii=False)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @registry.tool('read_skill', 'Load a saved skill by name and mark it used. Returns its Markdown and revision.',
                   {'type': 'object', 'properties': {'name': {'type': 'string'}}, 'required': ['name']}, category='skills')
    def read_skill(ctx, name):
        try:
            return json.dumps(ctx.workspace.skills.use(name), ensure_ascii=False)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @registry.tool('save_skill', 'Save or update a reusable workflow after it has been verified. Include when to use it, '
                   'numbered steps, pitfalls and verification. Use generic placeholders, never secrets or private data. '
                   'For updates pass the revision returned by read_skill.',
                   {'type': 'object', 'properties': {
                       'name': {'type': 'string', 'description': 'lowercase-with-hyphens'},
                       'description': {'type': 'string', 'description': 'One line: when to use it'},
                       'content': {'type': 'string', 'description': 'Markdown body'},
                       'revision': {'type': 'string', 'description': 'Existing revision when updating; omit for new'}},
                    'required': ['name', 'description', 'content']}, category='skills', mutates=True)
    def save_skill(ctx, name, description, content, revision=''):
        try:
            result = ctx.workspace.skills.save(name, description, content, revision or '')
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return 'Saved skill ' + result['name'] + ' (revision ' + result['revision'][:12] + ').'
