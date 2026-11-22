from dataclasses import dataclass
import hashlib
import json


@dataclass(frozen=True, slots=True)
class Envelope:
    identity: str
    body: dict


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('ascii')


def content_identity(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def require_fields(value: object, fields: set[str], label: str) -> dict:
    if type(value) is not dict or value.keys() != fields:
        raise ValueError(f'invalid {label} fields')
    return value


def require_identity(value: object) -> None:
    if (type(value) is not str or len(value) != 64
            or any(char not in '0123456789abcdef' for char in value)):
        raise ValueError('invalid content identity')


def encode_envelope(body: dict) -> bytes:
    if type(body) is not dict:
        raise ValueError('handoff body must be an object')
    return _canonical_json({
        'version': 1, 'kind': 'watershed-handoff',
        'identity': content_identity(body), 'body': body,
    })


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate exchange field {key}')
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f'invalid JSON number {value}')


def decode_envelope(raw: bytes) -> Envelope:
    if type(raw) is not bytes:
        raise ValueError('expected original exchange bytes')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant)
    except (ValueError, RecursionError) as error:
        raise ValueError('invalid exchange JSON') from error
    frame = require_fields(value, {'version', 'kind', 'identity', 'body'}, 'exchange envelope')
    if type(frame['version']) is not int or frame['version'] != 1 or frame['kind'] != 'watershed-handoff':
        raise ValueError('unsupported exchange envelope')
    body = frame['body']
    if type(body) is not dict:
        raise ValueError('handoff body must be an object')
    require_identity(frame['identity'])
    if content_identity(body) != frame['identity']:
        raise ValueError('exchange content identity mismatch')
    return Envelope(frame['identity'], body)
