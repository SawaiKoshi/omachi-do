// Inert, action-specific contracts. Existing call validation is unchanged.
import { validCallReceipt } from './staff-call.mjs';
const uuid = value => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)
  && value !== '00000000-0000-0000-0000-000000000000';
const integer = value => Number.isSafeInteger(value) && value >= 0;
const stamp = value => typeof value === 'string' && /(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value));
const keys = (value, names) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).sort().join(',') === [...names].sort().join(',');
const RECEIPT_KEYS = ['operation_id', 'reception_id', 'action', 'outcome', 'response_status', 'recorded_at', 'resulting_revision', 'reception', 'error'];


export const operationLabResponse = response => response.headers?.get('X-Omachi-Preview') === 'synthetic'
  && response.headers?.get('X-Omachi-Preview-Actions') === 'call,absent';
export const operationLabMetadata = value => value?.schema_version === 2 && uuid(value.database_id) && uuid(value.write_epoch)
  && value.staff_write_mode === 'revision_required' && value.write_enabled === true && value.seat_corrections_enabled === false;

function validAbsentReception(value) {
  if (!keys(value, ['id', 'display_number', 'reception_order', 'received_at', 'adults', 'children', 'total_people',
    'status', 'called_at', 'seated_at', 'cancelled_at', 'actual_wait_minutes', 'snapshot', 'prediction'])) return false;
  const s = value.snapshot, p = value.prediction;
  return uuid(value.id) && typeof value.display_number === 'string' && value.display_number.length > 0
    && integer(value.reception_order) && value.reception_order > 0 && stamp(value.received_at)
    && integer(value.adults) && integer(value.children) && value.total_people === value.adults + value.children
    && value.total_people >= 1 && value.total_people <= 100 && value.status === 'absent' && stamp(value.called_at)
    && Date.parse(value.called_at) >= Date.parse(value.received_at)
    && value.seated_at === null && value.cancelled_at === null && value.actual_wait_minutes === null
    && keys(s, ['captured_at', 'source', 'waiting_groups', 'ahead_groups', 'ahead_same_size_groups', 'party_size_groups'])
    && stamp(s.captured_at) && typeof s.source === 'string'
    && ['waiting_groups', 'ahead_groups', 'ahead_same_size_groups'].every(k => integer(s[k]))
    && keys(s.party_size_groups, ['1-2', '3-4', '5+']) && Object.values(s.party_size_groups).every(integer)
    && keys(p, ['status', 'minutes', 'model_version', 'predicted_at', 'warnings']) && stamp(p.predicted_at)
    && ['available', 'model_unavailable', 'no_waiting_groups', 'unsupported_weekday', 'unsupported_time', 'prediction_failed'].includes(p.status)
    && (p.status === 'available' ? typeof p.minutes === 'number' && Number.isFinite(p.minutes) && p.minutes >= 0
      && typeof p.model_version === 'string' && p.model_version.length > 0 : p.minutes === null)
    && (p.model_version === null || typeof p.model_version === 'string')
    && Array.isArray(p.warnings) && p.warnings.every(v => typeof v === 'string');
}

function validAbsentReceipt(value, record) {
  if (!keys(value, RECEIPT_KEYS) || value.operation_id !== record.operation_id || value.reception_id !== record.reception_id
      || value.action !== 'absent' || !stamp(value.recorded_at)
      || !(value.resulting_revision === null || integer(value.resulting_revision))) return false;
  if (value.outcome === 'rejected') {
    return value.reception === null && keys(value.error, ['code', 'detail'])
      && typeof value.error.detail === 'string' && value.error.detail.trim().length > 0
      && ['reception_not_found', 'revision_conflict', 'write_epoch_conflict', 'state_conflict'].includes(value.error.code)
      && value.response_status === (value.error.code === 'reception_not_found' ? 404 : 409);
  }
  if (!['state_changed', 'no_change'].includes(value.outcome) || value.response_status !== 200 || value.error !== null
      || !validAbsentReception(value.reception) || value.reception.id !== record.reception_id || !integer(value.resulting_revision)) return false;
  if (value.outcome === 'state_changed' && value.resulting_revision === 0) return false;
  return !record.request || value.resulting_revision === record.request.expected_revision + (value.outcome === 'state_changed' ? 1 : 0);
}


export const operationAction = record => record?.action ?? record?.request?.action ?? (record?.version === 1 ? 'call' : null);
const stableFields = ['id', 'display_number', 'reception_order', 'received_at', 'adults', 'children', 'total_people'];
const canonical = value => value && typeof value === 'object'
  ? Array.isArray(value) ? value.map(canonical) : Object.fromEntries(Object.keys(value).sort().map(k => [k, canonical(value[k])])) : value;
export const contractSignature = value => JSON.stringify(canonical(value));

export function validOperationReceipt(value, record, before = null) {
  const action = operationAction(record);
  if (!['call', 'absent'].includes(action) || record.request && record.request.action !== action
      || !(action === 'call' ? validCallReceipt(value, record) : validAbsentReceipt(value, record))) return false;
  if (value.outcome === 'rejected' || !before) return true;
  const row = value.reception;
  if (!stableFields.every(k => row[k] === before[k])) return false;
  for (const k of ['snapshot', 'prediction']) {
    if (Object.hasOwn(before, k) && contractSignature(row[k]) !== contractSignature(before[k])) return false;
  }
  if ((action === 'absent' || value.outcome === 'no_change') && row.called_at !== before.called_at) return false;
  if (value.outcome === 'state_changed') {
    return action === 'call' ? ['waiting', 'absent'].includes(before.status) : before.status === 'called';
  }
  return before.status === (action === 'call' ? 'called' : 'absent');
}

export function validOperationContext(value, id) {
  const row = value?.reception, meta = value?.metadata;
  return operationLabMetadata(meta) && typeof meta.view_version === 'string' && meta.view_version.length > 0
    && row?.id === id && uuid(id) && integer(row.revision)
    && typeof row.display_number === 'string' && row.display_number.length > 0 && integer(row.reception_order) && row.reception_order > 0
    && stamp(row.received_at) && integer(row.adults) && integer(row.children)
    && row.total_people === row.adults + row.children && row.total_people >= 1 && row.total_people <= 100
    && ['waiting', 'called', 'absent', 'seated', 'cancelled'].includes(row.status)
    && ['called_at', 'seated_at', 'cancelled_at'].every(k => row[k] === null || stamp(row[k]))
    && (!['called', 'absent'].includes(row.status) || stamp(row.called_at))
    && (row.actual_wait_minutes === null || typeof row.actual_wait_minutes === 'number' && Number.isFinite(row.actual_wait_minutes) && row.actual_wait_minutes >= 0)
    && Array.isArray(row.allowed_actions) && row.allowed_actions.every(v => ['call', 'absent', 'return', 'seat', 'cancel'].includes(v));
}

export const canApplyOperation = (action, row) => ['call', 'absent'].includes(action)
  && row.allowed_actions.includes(action)
  && (action === 'call' ? ['waiting', 'absent'].includes(row.status) : row.status === 'called');

// Historical receipts are never returned as the current row. Advancing beyond
// a successful operation is valid; a same-revision status disagreement is not.
export function receiptMatchesCurrent(receipt, current) {
  if (receipt.resulting_revision !== null && current.revision < receipt.resulting_revision) return false;
  if (!receipt.reception) return true;
  if (!stableFields.every(k => receipt.reception[k] === current[k])) return false;
  return current.revision !== receipt.resulting_revision
    || ['status', 'called_at', 'seated_at', 'cancelled_at', 'actual_wait_minutes'].every(k => receipt.reception[k] === current[k]);
}
