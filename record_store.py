"""JSON locally; atomic Upstash REST commands when Redis is configured."""
import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests


class StorageError(RuntimeError):
    """Safe to expose without leaking URLs, credentials, or records."""


_LOCK = threading.RLock()
_LEASES = {}
_PREFIX = "eticket:{tickets}:"
_INDEX = _PREFIX + "index"
_SEQUENCE = _PREFIX + "sequence"
_RECORDS = _PREFIX + "record:"
CONTACTS_FILE = Path(os.environ.get("CONTACTS_FILE", "telegram_contacts.json"))


def validate_configuration() -> None:
    url = os.environ.get("UPSTASH_REDIS_REST_URL", "")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
    if not url and not token:
        if os.environ.get("VERCEL"):
            raise StorageError("Durable storage is required on Vercel")
        return
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "https" and parsed.hostname and not parsed.username
                 and not parsed.password and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid or not token:
        raise StorageError("Invalid durable storage configuration")


def _redis() -> bool:
    validate_configuration()
    return bool(os.environ.get("UPSTASH_REDIS_REST_URL"))


def _command(*args):
    validate_configuration()
    try:
        response = requests.post(
            os.environ["UPSTASH_REDIS_REST_URL"].rstrip("/"),
            headers={"Authorization": "Bearer " + os.environ["UPSTASH_REDIS_REST_TOKEN"]},
            json=list(args), timeout=(3, 10),
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or "error" in body or "result" not in body:
            raise ValueError()
        return body["result"]
    except (requests.RequestException, ValueError, KeyError, TypeError):
        raise StorageError("Durable storage operation failed") from None


# The index and per-record hashes share a Redis cluster hash tag. Every read or
# mutation is a single script, so no read/modify/write round trips can lose data.
_RECORD_SCRIPT = """
local index, seq, prefix = KEYS[1], KEYS[2], KEYS[3]
local op = ARGV[1]
local function read(id)
  local value = redis.call('HGET', prefix .. id, 'json')
  if value then return cjson.decode(value) end
end
local function write(record)
  redis.call('HSET', prefix .. record.id, 'json', cjson.encode(record))
end
if op == 'load' then
  local result = {}
  for _, id in ipairs(redis.call('ZRANGE', index, 0, -1)) do
    local record = read(id)
    if record then table.insert(result, record) end
  end
  if #result == 0 then return '[]' end
  return cjson.encode(result)
elseif op == 'save' or op == 'import' then
  if op == 'import' and (redis.call('EXISTS', index) == 1 or
      redis.call('EXISTS', seq) == 1) then return 0 end
  if op == 'import' then
    local cursor = '0'
    repeat
      local scan = redis.call('SCAN', cursor, 'MATCH', prefix .. '*', 'COUNT', 100)
      cursor = scan[1]
      if #scan[2] > 0 then return 0 end
    until cursor == '0'
  end
  local records = cjson.decode(ARGV[2])
  for _, id in ipairs(redis.call('ZRANGE', index, 0, -1)) do
    redis.call('DEL', prefix .. id)
  end
  redis.call('DEL', index, seq)
  for _, record in ipairs(records) do
    write(record)
    redis.call('ZADD', index, redis.call('INCR', seq), record.id)
  end
  return 1
elseif op == 'append' then
  local record = cjson.decode(ARGV[2])
  if redis.call('HEXISTS', prefix .. record.id, 'json') == 1 then return 0 end
  write(record)
  redis.call('ZADD', index, redis.call('INCR', seq), record.id)
  return 1
elseif op == 'delete' then
  local removed = redis.call('DEL', prefix .. ARGV[2])
  redis.call('ZREM', index, ARGV[2])
  return removed
elseif op == 'update' or op == 'token' then
  local record = read(ARGV[2])
  if not record then return false end
  local fields = cjson.decode(ARGV[3])
  for key, value in pairs(fields) do
    if op ~= 'token' or not record[key] or record[key] == cjson.null or record[key] == '' then
      record[key] = value
    end
  end
  write(record)
  return cjson.encode(record)
end
"""


def _eval_records(op, *args):
    return _command("EVAL", _RECORD_SCRIPT, 3, _INDEX, _SEQUENCE, _RECORDS, op, *args)


def validate_records(records):
    if not isinstance(records, list):
        raise StorageError("Records must be a list")
    ids = set()
    for record in records:
        if (not isinstance(record, dict) or not isinstance(record.get("id"), str)
                or not record["id"].strip() or record["id"] in ids):
            raise StorageError("Records require unique nonempty string IDs")
        ids.add(record["id"])
    try:
        return json.dumps(records, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError):
        raise StorageError("Records must contain valid JSON values") from None


def _read_local(path):
    try:
        if not Path(path).is_file():
            return []
        records = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(records, list):
            raise ValueError()
        return records
    except (OSError, ValueError):
        raise StorageError("Local record read failed") from None


def _write_local(path, records, message="Local record write failed"):
    temporary = None
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         delete=False) as stream:
            temporary = stream.name
            json.dump(records, stream, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(temporary, path)
    except (OSError, ValueError, TypeError):
        raise StorageError(message) from None
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def load_records(path):
    if _redis():
        try:
            records = json.loads(_eval_records("load"))
            validate_records(records)
            return records
        except (ValueError, TypeError):
            raise StorageError("Invalid durable storage response") from None
    with _LOCK:
        return _read_local(path)


def save_records(path, records):
    payload = validate_records(records)
    if _redis():
        _eval_records("save", payload)
    else:
        with _LOCK:
            _write_local(path, records)


def append_record(path, record):
    payload = validate_records([record])
    if _redis():
        if _eval_records("append", json.dumps(record, allow_nan=False)) != 1:
            raise StorageError("Record ID already exists")
    else:
        with _LOCK:
            records = _read_local(path)
            if any(r.get("id") == record["id"] for r in records):
                raise StorageError("Record ID already exists")
            _write_local(path, records + json.loads(payload))


def update_record(path, record_id, *, only_missing=False, **fields):
    if "id" in fields and fields["id"] != record_id:
        raise StorageError("Record ID cannot change")
    try:
        payload = json.dumps(fields, allow_nan=False)
    except (ValueError, TypeError):
        raise StorageError("Fields must contain valid JSON values") from None
    if _redis():
        result = _eval_records("token" if only_missing else "update", record_id, payload)
        try:
            return json.loads(result) if result is not None and result is not False else None
        except (ValueError, TypeError):
            raise StorageError("Invalid durable storage response") from None
    with _LOCK:
        records = _read_local(path)
        for record in records:
            if record.get("id") == record_id:
                record.update({k: v for k, v in fields.items()
                               if not only_missing or not record.get(k)})
                _write_local(path, records)
                return record
        return None


def delete_record(path, record_id):
    if _redis():
        return bool(_eval_records("delete", record_id))
    with _LOCK:
        records = _read_local(path)
        remaining = [r for r in records if r.get("id") != record_id]
        if len(remaining) == len(records):
            return False
        _write_local(path, remaining)
        return True


def import_records(records):
    """Atomically import into an empty Redis destination; never overwrite."""
    payload = validate_records(records)
    if not _redis():
        raise StorageError("Migration requires durable storage configuration")
    if _eval_records("import", payload) != 1:
        raise StorageError("Migration destination is not empty")


_LEASE_SCRIPT = """
local lease, done = KEYS[1], KEYS[2]
local op, owner = ARGV[1], ARGV[2]
if op == 'acquire' then
  if redis.call('EXISTS', done) == 1 then return 'completed' end
  if redis.call('SET', lease, owner, 'NX', 'EX', 90) then return 'acquired' end
  return 'processing'
end
if redis.call('GET', lease) ~= owner then return 0 end
if op == 'complete' then redis.call('SET', done, '1', 'EX', 604800) end
redis.call('DEL', lease)
return 1
"""


def _lease(op, bot_id, update_id, owner):
    if not owner:
        raise StorageError("Lease owner is required")
    identity = hashlib.sha256((str(bot_id) + ":" + str(update_id)).encode()).hexdigest()
    if _redis():
        key = _PREFIX + "update:" + identity
        return _command("EVAL", _LEASE_SCRIPT, 2, key, key + ":done", op, owner)
    with _LOCK:
        now = time.monotonic()
        for key in list(_LEASES):
            if _LEASES[key][2] <= now:
                del _LEASES[key]
        current = _LEASES.get(identity)
        if op == "acquire":
            if current:
                return "completed" if current[0] == "done" else "processing"
            if len(_LEASES) >= 10000:
                raise StorageError("Local update tracking capacity reached")
            _LEASES[identity] = ("lease", owner, now + 90)
            return "acquired"
        if current and current[:2] == ("lease", owner):
            if op == "complete":
                _LEASES[identity] = ("done", owner, now + 604800)
            else:
                del _LEASES[identity]


def acquire_update(bot_id: str, update_id: int, owner: str) -> str:
    result = _lease("acquire", bot_id, update_id, owner)
    if result not in ("acquired", "processing", "completed"):
        raise StorageError("Invalid durable storage response")
    return result


def complete_update(bot_id: str, update_id: int, owner: str) -> None:
    _lease("complete", bot_id, update_id, owner)


def release_update(bot_id: str, update_id: int, owner: str) -> None:
    _lease("release", bot_id, update_id, owner)


_CONTACTS = _PREFIX + "contacts"


def _username(username):
    if not isinstance(username, str):
        raise StorageError("Invalid contact username")
    normalized = username.strip()
    if normalized.startswith("@"):
        normalized = normalized[1:]
    normalized = normalized.casefold()
    if not normalized:
        raise StorageError("Invalid contact username")
    return normalized


def _chat_id(chat_id):
    if isinstance(chat_id, bool) or not isinstance(chat_id, (str, int)):
        raise StorageError("Invalid contact chat ID")
    chat_id = str(chat_id).strip()
    if not chat_id:
        raise StorageError("Invalid contact chat ID")
    return chat_id


def _local_contacts():
    try:
        if not CONTACTS_FILE.is_file():
            return {}
        contacts = json.loads(CONTACTS_FILE.read_text(encoding="utf-8"))
        if (not isinstance(contacts, dict) or
                any(not isinstance(k, str) or not isinstance(v, str)
                    for k, v in contacts.items())):
            raise ValueError()
        return contacts
    except (OSError, ValueError):
        raise StorageError("Local contact read failed") from None


def remember_contact(username: str, chat_id: str) -> None:
    """Remember a username observed in a private bot conversation."""
    username = _username(username)
    chat_id = _chat_id(chat_id)
    if _redis():
        result = _command("HSET", _CONTACTS, username, chat_id)
        if result not in (0, 1):
            raise StorageError("Invalid durable storage response")
        return
    with _LOCK:
        contacts = _local_contacts()
        contacts[username] = chat_id
        _write_local(CONTACTS_FILE, contacts, "Local contact write failed")


def find_contact(username: str) -> str | None:
    username = _username(username)
    if _redis():
        result = _command("HGET", _CONTACTS, username)
        if result is not None and not isinstance(result, str):
            raise StorageError("Invalid durable storage response")
        return result
    with _LOCK:
        return _local_contacts().get(username)
