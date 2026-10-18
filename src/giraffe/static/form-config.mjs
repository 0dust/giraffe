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

function validBucketLevels(levels) {
  return Array.isArray(levels) && levels.length >= 1 && levels.length <= 16 &&
    new Set(levels).size === levels.length &&
    levels.every(level => Number.isInteger(level) && level >= 1 && level <= 256);
}

export function inferSetupPreset(config) {
  const checks = config.checks;
  const matches = expected => Array.isArray(checks) && checks.length === expected.length &&
    new Set(checks).size === checks.length && expected.every(id => checks.includes(id));
  if (matches(['access','correctness'])) return 'answers';
  if (matches(['access','correctness','capacity'])) return 'arrival';
  if (matches(['access','buckets']) && validBucketLevels(config.buckets?.levels) &&
      config.concurrency === Math.max(...config.buckets.levels) &&
      checks.every(id => !Object.keys(config.test_options?.[id] || {}).length)) return 'concurrent';
  return 'custom';
}

export function applySetupPreset(source, preset, defaults) {
  const config = structuredClone(source);
  const selection = {
    answers:['access','correctness'],
    concurrent:['access','buckets'],
    arrival:['access','correctness','capacity'],
  }[preset];
  if (selection) config.checks = selection;
  config.structured_json = config.checks.includes('json');
  config.metrics = config.checks.includes('gpu');
  config.tool_calling = config.checks.includes('tools');
  if (preset === 'concurrent') {
    if (!config.buckets) {
      config.buckets = structuredClone(defaults);
      config.buckets.samples_per_bucket = Math.max(
        config.buckets.samples_per_bucket, config.limits?.min_samples ?? 5);
      for (const size of Object.keys(config.buckets.output_tokens)) {
        config.buckets.output_tokens[size] = Math.min(config.buckets.output_tokens[size], config.max_output_tokens);
      }
    }
    if (validBucketLevels(config.buckets.levels)) config.concurrency = Math.max(...config.buckets.levels);
  }
  return config;
}

// Basic concurrent testing has one load control: the tested levels. Advanced
// configurations retain their independently chosen run ceiling.
export function configureSetupLoad(source, preset, {allowInvalid=false}={}) {
  const config = structuredClone(source);
  if (preset !== 'concurrent') return config;
  if (!validBucketLevels(config.buckets?.levels)) {
    if (allowInvalid) return config;
    throw new Error('Concurrency levels must contain 1 to 16 distinct whole numbers from 1 to 256.');
  }
  config.concurrency = Math.max(...config.buckets.levels);
  return config;
}

export function configureBuckets(source, values, defaults, {allowInvalid=false}={}) {
  const config = structuredClone(source);
  const fields = ['levels','samples_per_bucket','window_seconds','mixed_pairs','heavy_long_input','heavy_long_output',
    ...['input','output'].flatMap(kind => ['short','medium','long'].map(size => `${kind}_${size}`))];
  if (!fields.some(name => values.has(`buckets_${name}`))) return config;
  const settings = structuredClone(source.buckets || defaults);
  if (values.has('buckets_levels')) {
    settings.levels = String(values.get('buckets_levels') || '').trim().split(/[\s,]+/)
      .filter(Boolean).map(value => numeric(value, 'Concurrency levels', allowInvalid));
    if (!allowInvalid && !validBucketLevels(settings.levels)) {
      throw new Error('Concurrency levels must contain 1 to 16 distinct whole numbers from 1 to 256.');
    }
  }
  for (const name of ['samples_per_bucket','window_seconds','mixed_pairs']) {
    if (values.has(`buckets_${name}`)) settings[name] = numeric(values.get(`buckets_${name}`), name, allowInvalid);
  }
  for (const [kind, group] of [['input','input_chars'],['output','output_tokens']]) {
    for (const size of ['short','medium','long']) {
      const name = `buckets_${kind}_${size}`;
      if (!values.has(name)) continue;
      const label = `${size} ${kind === 'input' ? 'question target' : 'answer cap'}`;
      const value = numeric(values.get(name), label, true);
      if (!allowInvalid && (!Number.isInteger(value) || value < 1 || value > 65536)) {
        throw new Error(`${label} must be a whole number from 1 to 65536.`);
      }
      settings[group] ||= structuredClone(defaults[group]);
      settings[group][size] = value;
    }
  }
  for (const name of ['long_input','long_output']) {
    if (values.has(`buckets_heavy_${name}`)) {
      settings.heavy_weights ||= structuredClone(defaults.heavy_weights);
      settings.heavy_weights[name] = numeric(values.get(`buckets_heavy_${name}`), `${name} weight`, allowInvalid);
    }
  }
  if (!allowInvalid && config.checks.some(id => ['buckets','mixed'].includes(id)) &&
      Object.values(settings.output_tokens).some(cap => cap > config.max_output_tokens)) {
    throw new Error(`Each answer cap must be ${config.max_output_tokens} tokens or less. Lower the answer cap or edit Maximum answer length under Shared run settings → Request defaults.`);
  }
  if (source.buckets || config.checks.some(id => ['buckets','mixed'].includes(id)) ||
      !sameConfig(settings, defaults)) config.buckets = settings;
  return config;
}

// A level needs at least one full concurrent batch for each of the nine size pairs.
// This is bucket traffic only; other selected checks share the same request budget.
export function bucketCoveragePlan(config) {
  if (!config.checks.includes('buckets') || !config.buckets) return null;
  const {levels, samples_per_bucket: samples} = config.buckets;
  if (!levels.length || levels.some(level => !Number.isInteger(level) || level < 1 || level > 256) ||
      !Number.isInteger(samples) || samples < 2) return null;
  const perTarget = 9 * levels.reduce((total, level) => total + Math.max(samples, level), 0);
  return {levels, peak:Math.max(...levels), perTarget, total:perTarget * config.targets.length};
}

export function configureTests(source, values, checks, trafficDefaults, {allowEmpty=false, allowInvalid=false, percentageInputs=false}={}) {
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
    const previous = source.test_options?.[check.id] || {};
    const options = {};
    for (const name of check.option_fields) {
      if (config.request_source === 'request_pack' && check.id === 'correctness' && ['samples','concurrency'].includes(name)) {
        if (Object.hasOwn(previous, name)) options[name] = previous[name];
        continue;
      }
      const raw = values.get(`test_${check.id}_${name}`);
      const value = ['trace_file','server_log','backend_verification'].includes(name) ? (String(raw || '').trim() || null) : numeric(raw, name, allowInvalid);
      // A saved override still owns its value when the global setting happens
      // to match it. Blank options and Reset remain explicit ways to inherit.
      if (value !== null && (value !== optionDefault(config, check, name) ||
          (Object.hasOwn(previous, name) && previous[name] != null))) options[name] = value;
    }
    const limits = {};
    for (const name of check.limit_fields) {
      let value = numeric(values.get(`test_${check.id}_limit_${name}`), name, allowInvalid);
      if (percentageInputs && ['min_correctness','max_error_rate'].includes(name) && typeof value === 'number') value /= 100;
      const inherited = Object.hasOwn(config.limits || {}, name) ?
        config.limits[name] : check.defaults.limits[name];
      if (value !== inherited || Object.hasOwn(previous.limits || {}, name)) limits[name] = value;
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

export function effectiveProfileDefaults(config, defaults, key) {
  const profile = structuredClone(defaults[key] || {});
  if (key === 'sessions') {
    profile.concurrency = Math.min(profile.concurrency, config.concurrency);
    profile.max_tokens = Math.min(profile.max_tokens, config.max_output_tokens);
  }
  if (key === 'consistency') profile.levels = (profile.levels || []).filter(level => level <= config.concurrency);
  return profile;
}

export function parseLogprobSettings(raw) {
  if (raw.length > 4096) throw new Error('Logprob settings must fit within 4096 characters.');
  let value;
  try { value = JSON.parse(raw); } catch { throw new Error('Logprob settings must be a JSON object or null.'); }
  if (value !== null && (typeof value !== 'object' || Array.isArray(value))) throw new Error('Logprob settings must be a JSON object or null.');
  return value;
}

export function parseToolFixtures(raw) {
  if (raw.length > 65536) throw new Error('Tool fixtures must fit within 65,536 characters.');
  let fixtures;
  try { fixtures = JSON.parse(raw); }
  catch { throw new Error('Tool fixtures must be a JSON array. Use [] for an empty list.'); }
  if (!Array.isArray(fixtures) || fixtures.some(item => !item || typeof item !== 'object' || Array.isArray(item))) {
    throw new Error('Tool fixtures must be a JSON array of fixture objects.');
  }
  if (fixtures.length > 16) throw new Error('Configure at most 16 tool fixtures.');
  return fixtures;
}

// Catalog profile forms patch only fields the operator edited. Viewing a null
// profile or filtering a check must not materialize defaults or erase JSON data.
export function configureProfiles(source, values, defaults, {allowInvalid=false}={}) {
  const config = structuredClone(source);
  const arrayFields = new Set(['modes', 'shapes', 'levels']);
  const textFields = new Set(['mode', 'equality']);
  for (const key of ['arrivals', 'prefix', 'sessions', 'consistency', 'tools']) {
    const edits = JSON.parse(values.get(`profile_edits_${key}`) || '[]');
    if (!edits.length) continue;
    if (key === 'tools') {
      if (edits.includes('forced_tool_diagnostic')) {
        config.forced_tool_diagnostic = values.get('profile_tools_forced_tool_diagnostic') === 'true';
      }
      if (edits.some(name => ['modes', 'fixtures', 'conversations'].includes(name))) {
        const profile = structuredClone(config.tool_checks || {fixtures: [], modes: null});
        if (edits.includes('modes')) {
          const raw = String(values.get('profile_tools_modes') || '');
          if (!['', 'complete', 'stream', 'complete,stream', 'stream,complete'].includes(raw)) {
            throw new Error('Choose complete, streaming, both, or the run default for tool requests.');
          }
          profile.modes = raw ? raw.split(',') : null;
        }
        for (const name of ['fixtures', 'conversations']) {
          if (!edits.includes(name)) continue;
          const raw = String(values.get(`profile_tools_${name}`) ?? '[]');
          try { profile[name] = parseToolFixtures(raw); }
          catch (error) {
            if (!allowInvalid) throw error;
            profile[name] = raw;
          }
        }
        config.tool_checks = profile;
      }
      continue;
    }
    const profile = structuredClone(config[key] || effectiveProfileDefaults(config, defaults, key));
    for (const name of edits) {
      if (!Object.hasOwn(defaults[key], name)) continue;
      const raw = values.get(`profile_${key}_${name}`);
      if (key === 'consistency' && name === 'logprobs') {
        try { profile[name] = parseLogprobSettings(String(raw || 'null')); }
        catch (error) { if (!allowInvalid) throw error; profile[name] = String(raw); }
      } else if (arrayFields.has(name)) {
        profile[name] = String(raw || '').split(',').map(value => value.trim()).filter(Boolean);
        if (name === 'levels') profile[name] = profile[name].map(value => numeric(value, 'Concurrency levels', allowInvalid));
      } else if (name === 'turns' || name === 'saved_answers') {
        profile[name] = String(raw || '').split('\n');
      } else if (name === 'strict') profile[name] = raw === 'true';
      else if (textFields.has(name)) profile[name] = String(raw || '');
      else profile[name] = numeric(raw, name, allowInvalid);
    }
    config[key] = profile;
  }
  if (!allowInvalid && typeof config.consistency?.logprobs === 'string') config.consistency.logprobs = parseLogprobSettings(config.consistency.logprobs);
  for (const name of ['fixtures', 'conversations']) {
    if (!allowInvalid && typeof config.tool_checks?.[name] === 'string') {
      config.tool_checks[name] = parseToolFixtures(config.tool_checks[name]);
    }
  }
  if (!allowInvalid && config.tool_calling && config.tool_checks?.modes?.includes('stream') && !config.stream) {
    throw new Error('Enable streaming in Run limits or choose complete tool requests.');
  }
  return config;
}
