// Keep form serialization independent of the DOM so selection and inheritance
// have the same behavior for ordinary edits, reruns, and advanced JSON.
export function sameConfig(left, right) {
  if (left === right) return true;
  if (!left || !right || typeof left !== 'object' || typeof right !== 'object') return false;
  const keys = Object.keys(left);
  return keys.length === Object.keys(right).length && keys.every(key =>
    Object.hasOwn(right, key) && sameConfig(left[key], right[key]));
}

export function optionDefault(config, check, name) {
  return config[name] ?? check.defaults[name];
}

export function optionValue(config, check, name) {
  return config.test_options?.[check.id]?.[name] ?? optionDefault(config, check, name);
}

export function limitValue(config, check, name) {
  const overrides = config.test_options?.[check.id]?.limits || {};
  return Object.hasOwn(overrides, name) ? overrides[name] :
    Object.hasOwn(config.limits || {}, name) ? config.limits[name] : check.defaults.limits[name];
}

function numeric(value, label, allowInvalid=false) {
  if (value == null || String(value).trim() === '') return null;
  const result = Number(value);
  if (!Number.isFinite(result)) {
    if (allowInvalid) return String(value);
    throw new Error(`${label} must be a finite number.`);
  }
  return result;
}

export function configureTests(source, values, checks, trafficDefaults, {allowEmpty=false, allowInvalid=false}={}) {
  const config = structuredClone(source);
  config.checks = values.getAll('check');
  if (!config.checks.length && !allowEmpty) throw new Error('Select at least one test before running.');
  config.structured_json = config.checks.includes('json');
  config.metrics = config.checks.includes('gpu');
  config.tool_calling = config.checks.includes('tools');
  config.test_options ||= {};
  for (const check of checks) {
    if (values.get(`test_mode_${check.id}`) !== 'custom') {
      delete config.test_options[check.id];
      continue;
    }
    const options = {};
    for (const name of check.option_fields) {
      if (config.request_source === 'request_pack' && check.id === 'correctness' && ['samples','concurrency'].includes(name)) {
        if (Object.hasOwn(source.test_options?.correctness || {}, name)) options[name] = source.test_options.correctness[name];
        continue;
      }
      const raw = values.get(`test_${check.id}_${name}`);
      const value = name === 'trace_file' ? (String(raw || '').trim() || null) : numeric(raw, name, allowInvalid);
      if (value !== null && value !== optionDefault(config, check, name)) options[name] = value;
    }
    const limits = {};
    for (const name of check.limit_fields) {
      const value = numeric(values.get(`test_${check.id}_limit_${name}`), name, allowInvalid);
      const inherited = Object.hasOwn(config.limits || {}, name) ?
        config.limits[name] : check.defaults.limits[name];
      if (value !== inherited) limits[name] = value;
    }
    if (Object.keys(limits).length) options.limits = limits;
    if (Object.keys(options).length) config.test_options[check.id] = options;
    else delete config.test_options[check.id];
  }
  if (values.get('traffic_mode') === 'default') config.traffic = structuredClone(trafficDefaults);
  else {
    config.traffic = structuredClone(source.traffic || trafficDefaults);
    for (const name of Object.keys(trafficDefaults)) {
      if (name === 'mix') {
        for (const kind of Object.keys(trafficDefaults.mix)) {
          config.traffic.mix[kind] = numeric(values.get(`traffic_mix_${kind}`), `${kind} weight`, allowInvalid);
        }
      } else if (name === 'rates') {
        const parts = String(values.get('traffic_rates') || '').trim().split(/[\s,]+/);
        if (parts.some(part => !part) && !allowInvalid) throw new Error('Enter at least one arrival rate.');
        config.traffic.rates = parts.filter(Boolean).map(part => numeric(part, 'Arrival rate', allowInvalid));
      } else if (name === 'arrival') config.traffic.arrival = values.get('traffic_arrival');
      else config.traffic[name] = numeric(values.get(`traffic_${name}`), name, allowInvalid);
    }
  }
  if (config.request_source === 'request_pack' && source.traffic) {
    for (const name of ['mix','long_input_chars','long_output_words']) {
      if (Object.hasOwn(source.traffic, name)) config.traffic[name] = structuredClone(source.traffic[name]);
    }
  }
  return config;
}

// Only rendered judge fields participate; inactive drafts and JSON-only transport
// settings survive ordinary edits and source changes.
export function configureJudging(source, values, {allowInvalid=false}={}) {
  const config = structuredClone(source);
  if (!values.has('judge_count')) return config;
  const judging = config.judging ||= {};
  const previous = judging.judges || [];
  judging.judges = Array.from({length:Number(values.get('judge_count'))}, (_, index) => {
    const judge = {...previous[index]};
    for (const key of ['name','url','model','api_key_env']) {
      const value = String(values.get(`judge_${index}_${key}`) || '').trim();
      judge[key] = key === 'api_key_env' ? value || null : value;
    }
    judge.headers_env ||= {};
    return judge;
  });
  for (const key of ['concurrency','request_timeout_seconds','max_duration_seconds','max_output_tokens']) {
    judging[key] = numeric(values.get(`judging_${key}`), `Judge ${key}`, allowInvalid);
  }
  return config;
}

export function datasetCaseFromValues(previous, values) {
  const item = structuredClone(previous || {});
  item.fixture_id = String(values.get('example_id') || '').trim();
  item.messages = Array.from({length:Number(values.get('message_count'))}, (_, index) => ({
    role: values.get(`message_${index}_role`),
    content: String(values.get(`message_${index}_content`) || ''),
  }));
  item.scorer = values.get('example_scorer');
  const expected = String(values.get('example_expected') || '');
  delete item.expected;delete item.rubric;delete item.schema;
  if (item.scorer === 'json') {
    item.schema = JSON.parse(values.get('example_schema') || '{}');
    if (expected.trim()) item.expected = JSON.parse(expected);
  } else if (item.scorer !== 'none') item.expected = expected;
  const rubric = String(values.get('example_rubric') || '').trim();
  if (item.scorer === 'judge' && rubric) item.rubric = rubric;
  item.weight = numeric(values.get('example_weight'), 'Example weight');
  item.max_tokens = numeric(values.get('example_max_tokens'), 'Example output cap');
  item.options = JSON.parse(values.get('example_options') || '{}');
  return item;
}

export function changeDatasetScorer(item, scorer) {
  const result = structuredClone(item);
  if (scorer === item.scorer) return result;
  result.scorer = scorer;
  delete result.schema;
  if (scorer !== 'judge') delete result.rubric;
  if (scorer === 'none') delete result.expected;
  else {
    if (!Object.hasOwn(result, 'expected')) throw new Error(`Add an expected answer to ${item.fixture_id} first.`);
    if (typeof result.expected !== 'string') throw new Error(`Edit the expected answer for ${item.fixture_id} as text first.`);
    if (!result.expected.trim()) throw new Error(`Add an expected answer to ${item.fixture_id} first.`);
  }
  return result;
}

export function convertExpectedAnswer(value, from, to) {
  if (from === to || !value.trim()) return value;
  if (to === 'json') return JSON.stringify(value);
  let parsed;
  try { parsed = JSON.parse(value); }
  catch { throw new Error('Fix the JSON reference before changing the answer check.'); }
  if (typeof parsed !== 'string') throw new Error('Use a JSON string reference before switching to a text answer check.');
  return parsed;
}
