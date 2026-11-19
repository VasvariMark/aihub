"""Strict data contracts. Model output is always untrusted input."""
import json
import re

_CREDENTIAL = re.compile(r'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----|(?:sk-(?:proj-)?|ghp_|github_pat_|AIza)[A-Za-z0-9_-]{20,}')

ROLES = ('researcher', 'writer', 'coder', 'reviewer')
STATUSES = ('proposed', 'approved', 'running', 'awaiting_external', 'completed', 'failed', 'rejected')


class ValidationError(ValueError):
    pass


class ConflictError(ValueError):
    pass


def parse_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValidationError('Duplicate JSON field: ' + key)
            result[key] = value
        return result
    try:
        return json.loads(text, object_pairs_hook=pairs,
                          parse_constant=lambda x: (_ for _ in ()).throw(ValidationError('Non-finite JSON number')))
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValidationError('Invalid JSON: ' + str(exc)[:180]) from exc


def fields(value, required):
    if type(value) is not dict or set(value) != set(required):
        raise ValidationError('Expected exactly these fields: ' + ', '.join(required))


def text(value, name, maximum, minimum=1):
    if type(value) is not str or not minimum <= len(value.strip()) <= maximum:
        raise ValidationError(f'{name} must contain {minimum}–{maximum} characters.')
    if any(ord(ch) < 32 and ch not in '\n\r\t' for ch in value):
        raise ValidationError(name + ' contains control characters.')
    if _CREDENTIAL.search(value):
        raise ValidationError('Remove credential-like content before saving.')
    return value.strip()


def task_input(value):
    extra=('dependencies',) if type(value) is dict and 'dependencies' in value else ()
    fields(value, ('title', 'role', 'instructions', 'expected_output')+extra)
    if value['role'] not in ROLES:
        raise ValidationError('Unknown role. Choose researcher, writer, coder or reviewer.')
    result = dict(title=text(value['title'], 'Task title', 160), role=value['role'],
                instructions=text(value['instructions'], 'Instructions', 2400),
                expected_output=text(value['expected_output'], 'Expected output', 800))
    if extra:
        from .context import dependency_ids
        result['dependencies']=dependency_ids(value['dependencies'])
    return result


def validate_plan(value, *, require_dependencies=False):
    fields(value, ('title', 'summary', 'tasks'))
    if type(value['tasks']) is not list or not 1 <= len(value['tasks']) <= 8:
        raise ValidationError('A plan must contain 1–8 tasks.')
    tasks=[]
    for task in value['tasks']:
        if require_dependencies and (type(task) is not dict or 'depends_on' not in task):
            raise ValidationError('Every new task must explicitly declare depends_on (use [] if independent)')
        if type(task) is dict and 'dependencies' in task:
            raise ValidationError('Plans must use depends_on task numbers, not dependency IDs')
        if type(task) is dict and 'depends_on' in task:
            indices=task['depends_on']
            if type(indices) is not list or len(indices)>7 or any(type(i) is not int or not 1<=i<=len(value['tasks']) for i in indices) or len(set(indices))!=len(indices):
                raise ValidationError('Invalid dependency task numbers')
            cleaned=task_input({k:v for k,v in task.items() if k!='depends_on'})
            cleaned['depends_on']=indices
        else: cleaned=task_input(task)
        tasks.append(cleaned)
    def visit(index,path):
        if index in path: raise ValidationError('Circular dependency rejected')
        for dep in tasks[index].get('depends_on',[]): visit(dep-1,path|{index})
    for index in range(len(tasks)): visit(index,set())
    return dict(title=text(value['title'], 'Mission title', 160),
                summary=text(value['summary'], 'Plan summary', 1200),
                tasks=tasks)


def version(value):
    if type(value) is not int or value < 0:
        raise ValidationError('version must be a non-negative integer.')
    return value


def string_schema(limit):
    return {'type': 'string', 'minLength': 1, 'maxLength': limit}


TASK_SCHEMA = {'type': 'object', 'additionalProperties': False,
               'required': ['title', 'role', 'instructions', 'expected_output'],
               'properties': {'title': string_schema(160), 'role': {'type': 'string', 'enum': list(ROLES)},
                              'instructions': string_schema(2400), 'expected_output': string_schema(800)}}
PLAN_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['title', 'summary', 'tasks'],
               'properties': {'title': string_schema(160), 'summary': string_schema(1200),
                              'tasks': {'type': 'array', 'minItems': 1, 'maxItems': 8, 'items': TASK_SCHEMA}}}

# Planning uses 1-based task numbers, replaced by persistent IDs on save.
import copy
PLAN_SCHEMA_V2=copy.deepcopy(PLAN_SCHEMA)
PLAN_SCHEMA_V2['properties']['tasks']['items']['required'].append('depends_on')
PLAN_SCHEMA_V2['properties']['tasks']['items']['properties']['depends_on']={'type':'array','maxItems':7,'uniqueItems':True,'items':{'type':'integer','minimum':1,'maximum':8}}
