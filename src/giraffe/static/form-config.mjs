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
  if (name === 'concurrency') return inheritedConcurrency(config, config.default_concurrency ?? check.defaults[name]);
  if (['serving','first_output'].includes(check.id) && ['samples','max_output_tokens','request_timeout_seconds'].includes(name)) {
    const serving = config.checks?.includes('serving') ? config.test_options?.serving : null;
    const first = config.checks?.includes('first_output') ? config.test_options?.first_output : null;
    return serving?.[name] ?? (name !== 'samples' ? first?.[name] : null) ?? config[name] ?? check.defaults[name];
  }
  const rule = check.inheritance?.[name];
  if (rule?.maximum_default != null) return Math.min(rule.maximum_default, config[rule.suite_field] ?? check.defaults[name]);
  return config[name] ?? check.defaults[name];
}

function inheritedConcurrency(config, value) {
  return config.concurrency == null ? value : Math.max(1, Math.min(value,
    Math.floor(config.concurrency / (config.overlap_models ? config.targets?.length || 1 : 1))));
}

export function optionValue(config, check, name) {
  if (['serving','first_output'].includes(check.id) && ['samples','max_output_tokens','request_timeout_seconds'].includes(name)) {
    return optionDefault(config, check, name);
  }
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

// Checkboxes carry an explicit on/off sentinel because disabled inputs are not
// submitted by FormData. Empty enabled values remain invalid, rather than
// accidentally becoming an unchecked limit.
export function configureRunLimits(source, values, {allowInvalid=false}={}) {
  const config = structuredClone(source);
  for (const name of ['max_duration_seconds','max_requests','concurrency','max_memory_mb']) {
    if (!values.has(`${name}_enabled`)) continue;
    if (values.get(`${name}_enabled`) !== 'on') {
      config[name] = name === 'max_memory_mb' ? 0 : null;
      continue;
    }
    let value = numeric(values.get(name), name, allowInvalid);
    if (value === null) {
      const msg = 'Enter a value or turn this limit off.';
      if (!allowInvalid) throw Object.assign(new Error(msg), {errors:[{fields:[name],msg}]});
      value = '';
    }
    if (name === 'max_duration_seconds' && typeof value === 'number') {
      value *= {s:1,min:60,h:3600}[values.get('max_duration_unit') || 's'] ?? 1;
    }
    config[name] = value;
  }
  return config;
}

function validLoadLevels(levels, limits={}) {
  const bounds = limits['concurrent_load.schedule.levels'] || {}, item = bounds.items || {};
  return Array.isArray(levels) && levels.length >= (bounds.minItems ?? 1) && levels.length <= (bounds.maxItems ?? Infinity) &&
    new Set(levels).size === levels.length &&
    levels.every(level => Number.isInteger(level) && level >= (item.minimum ?? 1) && level <= (item.maximum ?? Infinity));
}

export function inferSetupPreset(config, {limits={}}={}) {
  const checks = config.checks;
  const matches = expected => Array.isArray(checks) && checks.length === expected.length &&
    new Set(checks).size === checks.length && expected.every(id => checks.includes(id));
  if (matches(['access','correctness'])) return 'answers';
  if (matches(['access','correctness','capacity'])) return 'arrival';
  if (matches(['access','concurrency']) && (config.concurrent_load?.schedule?.levels === undefined || validLoadLevels(config.concurrent_load.schedule.levels, limits)) &&
      checks.every(id => !Object.keys(config.test_options?.[id] || {}).length)) return 'concurrent';
  return 'custom';
}

export function applySetupPreset(source, preset, defaults) {
  const config = structuredClone(source);
  const selection = {
    answers:['access','correctness'],
    concurrent:['access','concurrency'],
    arrival:['access','correctness','capacity'],
  }[preset];
  if (selection) config.checks = selection;
  config.structured_json = config.checks.includes('json');
  config.metrics = config.checks.includes('gpu');
  config.tool_calling = config.checks.includes('tools');
  if (preset === 'concurrent') {
    config.concurrent_load ||= structuredClone(defaults);
  }
  return config;
}

// Tested levels and the optional suite ceiling are independent settings.
export function configureSetupLoad(source, preset, {allowInvalid=false, limits={}}={}) {
  const config = structuredClone(source);
  if (preset !== 'concurrent') return config;
  if (config.concurrent_load?.schedule?.levels !== undefined && !validLoadLevels(config.concurrent_load.schedule.levels, limits)) {
    if (allowInvalid) return config;
    throw new Error(loadLevelsMessage(limits));
  }
  return config;
}

function loadLevelsMessage(limits) {
  const bounds = limits['concurrent_load.schedule.levels'] || {}, item = bounds.items || {};
  return `Concurrency levels must contain ${bounds.minItems ?? 1}${bounds.maxItems == null ? ' or more' : ` to ${bounds.maxItems}`} distinct whole numbers from ${item.minimum ?? 1}${item.maximum == null ? ' upwards' : ` to ${item.maximum}`}.`;
}

export function configureConcurrentLoad(source, values, defaults, {allowInvalid=false, limits={}}={}) {
  const config = structuredClone(source);
  if (![...values.keys()].some(name => name.startsWith('load_'))) return config;
  const settings = structuredClone(defaults);
  for (const [group, value] of Object.entries(config.concurrent_load || {})) {
    settings[group] = value && typeof value === 'object' && !Array.isArray(value) ? {...settings[group], ...value} : value;
  }
  for (const size of ['short','medium','long']) {
    settings.profile[size] = {...defaults.profile[size], ...source.concurrent_load?.profile?.[size]};
  }
  const inheritedLevels = defaults.schedule.levels.filter(level => level <= inheritedConcurrency(config, Math.max(...defaults.schedule.levels)));
  if (!inheritedLevels.length) inheritedLevels.push(1);
  if (source.concurrent_load?.schedule?.levels === undefined) settings.schedule.levels = inheritedLevels;
  const validate = !allowInvalid && config.checks.includes('concurrency');
  const invalid = (fields, msg) => {throw Object.assign(new Error(msg), {errors:[{fields, msg}]});};
  const rendered = values.has('load_rendered');
  const sizes = ['short','medium','long'];
  const shapes = sizes.flatMap(input => sizes.map(output => `${input}_${output}`));
  const number = (group, key, label, {integer=false, nullable=false}={}) => {
    const name = `load_${group}_${key}`;
    if (!values.has(name)) return;
    const value = numeric(values.get(name), label, true);
    const bounds = limits[`concurrent_load.${group}.${key}`] || {};
    const minimum = bounds.minimum ?? bounds.exclusiveMinimum ?? -Infinity, maximum = bounds.maximum ?? Infinity;
    if (validate && !(nullable && value === null) &&
        (typeof value !== 'number' || !Number.isFinite(value) || value < minimum || value > maximum ||
         (bounds.exclusiveMinimum != null && value === bounds.exclusiveMinimum) ||
         (integer && !Number.isInteger(value)))) {
      const range = bounds.exclusiveMinimum != null ? `greater than ${minimum}${Number.isFinite(maximum)?` and at most ${maximum}`:''}` : Number.isFinite(maximum) ? `from ${minimum} to ${maximum}` : `of at least ${minimum}`;
      invalid([`concurrent_load.${group}.${key}`], `${label} must be ${integer ? 'a whole number' : 'a number'} ${range}.`);
    }
    settings[group] ||= {};
    settings[group][key] = value;
  };
  for (const size of sizes) for (const field of ['input_tokens','output_tokens']) {
    const name = `load_profile_${size}_${field}`;
    if (!values.has(name)) continue;
    const label = `${size[0].toUpperCase()+size.slice(1)} ${field === 'input_tokens' ? 'input target' : 'output cap'}`;
    const value = numeric(values.get(name), label, true);
    const bounds = limits[`concurrent_load.profile.${size}.${field}`] || {};
    if (validate && (!Number.isInteger(value) || value < (bounds.minimum ?? 1) || value > (bounds.maximum ?? Infinity))) {
      invalid([`concurrent_load.profile.${size}.${field}`], `${label} must be a whole number from ${bounds.minimum ?? 1}${bounds.maximum == null ? ' upwards' : ` to ${bounds.maximum}`}.`);
    }
    settings.profile[size][field] = value;
  }
  for (const type of ['combinations','mixed','interference','stress']) {
    const name = `load_${type}_enabled`;
    if (rendered || values.has(name)) settings[type].enabled = values.get(name) === 'on';
  }
  if (rendered || shapes.some(shape => values.has(`load_combinations_selected_${shape}`))) {
    const selected = shapes.filter(shape => values.get(`load_combinations_selected_${shape}`) === 'on');
    settings.combinations.selected = [...settings.combinations.selected.filter(shape => selected.includes(shape)),
      ...selected.filter(shape => !settings.combinations.selected.includes(shape))];
  }
  for (const shape of shapes) {
    const name = `load_mixed_shares_${shape}`;
    if (!rendered && !values.has(name)) continue;
    const raw = values.get(name);
    const value = raw == null || String(raw).trim() === '' ? 0 : numeric(raw, 'Share of requests', true);
    const bounds = limits[`concurrent_load.mixed.shares.${shape}`] || limits['concurrent_load.mixed.shares.*'] || {};
    if (validate && (typeof value !== 'number' || !Number.isFinite(value) || value < (bounds.minimum ?? 0) || value > (bounds.maximum ?? Infinity))) {
      invalid([`concurrent_load.mixed.shares.${shape}`], `Share of requests must be a number from ${bounds.minimum ?? 0}${bounds.maximum == null ? ' upwards' : ` to ${bounds.maximum}`}.`);
    }
    if (value === 0) delete settings.mixed.shares[shape];
    else settings.mixed.shares[shape] = value;
  }
  if (values.has('load_interference_trigger')) settings.interference.trigger = values.get('load_interference_trigger');
  number('interference','trigger_chunks','Chunk count',{integer:true});
  number('interference','max_slowdown','Maximum slowdown',{nullable:true});
  for (const key of ['large_input','large_output']) {
    if (rendered || values.has(`load_stress_${key}`)) settings.stress[key] = values.get(`load_stress_${key}`) === 'on';
  }
  number('stress','min_output_percent','Minimum output length');
  if (values.has('load_schedule_levels')) {
    settings.schedule.levels = String(values.get('load_schedule_levels') || '').trim().split(/[\s,]+/)
      .filter(Boolean).map(value => numeric(value, 'Concurrency levels', true));
    if (validate && !validLoadLevels(settings.schedule.levels, limits)) {
      invalid(['concurrent_load.schedule.levels'], loadLevelsMessage(limits));
    }
    if (validLoadLevels(settings.schedule.levels, limits)) settings.schedule.levels.sort((left,right) => left-right);
  }
  number('schedule','duration_seconds','Duration per workload',{integer:true});
  for (const key of ['first_output_p95_ms','response_p95_ms']) {
    number('pass_criteria',key,key === 'first_output_p95_ms' ? 'First answer p95' : 'Response p95',
      {integer:true,nullable:true});
  }
  for (const key of ['max_error_percent','min_correct_percent']) {
    number('pass_criteria',key,key === 'max_error_percent' ? 'Maximum errors' : 'Minimum correct answers');
  }
  number('pass_criteria','min_valid_responses','Minimum valid responses',{integer:true});
  if (validate) {
    if (!Object.values(settings).some(value => value?.enabled)) invalid(['concurrent_load.combinations.enabled'], 'Enable at least one load type.');
    if (settings.combinations.enabled && !settings.combinations.selected.length) invalid(['concurrent_load.combinations.selected'], 'Select at least one combination.');
    if (settings.mixed.enabled && Math.abs(Object.values(settings.mixed.shares).reduce((sum,share) => sum+share,0)-100) > 1e-6) {
      invalid([`concurrent_load.mixed.shares.${shapes.find(shape => settings.mixed.shares[shape] > 0) || shapes[0]}`], 'Mixed shares must equal 100%.');
    }
    if (settings.stress.enabled && !settings.stress.large_input && !settings.stress.large_output) invalid(['concurrent_load.stress.large_input'], 'Select at least one stress case.');
    if (settings.interference.max_slowdown !== null && settings.interference.max_slowdown <= 1) {
      invalid(['concurrent_load.interference.max_slowdown'], 'Maximum slowdown must be greater than 1, or blank to measure only.');
    }
  }
  config.concurrent_load = settings;
  if (source.concurrent_load?.schedule?.levels === undefined && sameConfig(settings.schedule.levels, inheritedLevels)) {
    delete settings.schedule.levels;
  }
  return config;
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
    const packCorrectness = config.request_source === 'request_pack' && check.id === 'correctness';
    if (!values.has(`test_mode_${check.id}`)) continue;
    if (values.get(`test_mode_${check.id}`) !== 'custom') {
      delete config.test_options[check.id];
      continue;
    }
    const previous = source.test_options?.[check.id] || {};
    const options = {...previous};
    for (const name of packCorrectness ? [] : check.option_fields) {
      if (!values.has(`test_${check.id}_${name}`)) continue;
      const raw = values.get(`test_${check.id}_${name}`);
      const value = ['trace_file','server_log','backend_verification'].includes(name) ? (String(raw || '').trim() || null) : numeric(raw, name, allowInvalid);
      if (value !== null && value === optionValue(source, check, name)) continue;
      // A saved override still owns its value when the global setting happens
      // to match it. Blank options and Reset remain explicit ways to inherit.
      if (value !== null && (value !== optionDefault(config, check, name) ||
          (Object.hasOwn(previous, name) && previous[name] != null))) options[name] = value;
      else delete options[name];
    }
    const limits = {...previous.limits};
    for (const name of check.limit_fields) {
      if (!values.has(`test_${check.id}_limit_${name}`)) continue;
      let value = numeric(values.get(`test_${check.id}_limit_${name}`), name, allowInvalid);
      if (percentageInputs && ['min_correctness','max_error_rate'].includes(name) && typeof value === 'number') value /= 100;
      const inherited = Object.hasOwn(config.limits || {}, name) ?
        config.limits[name] : check.defaults.limits[name];
      if (value !== inherited || Object.hasOwn(previous.limits || {}, name)) limits[name] = value;
      else delete limits[name];
    }
    if (Object.keys(limits).length) options.limits = limits;
    else delete options.limits;
    if (Object.keys(options).length) config.test_options[check.id] = options;
    else delete config.test_options[check.id];
  }
  if (values.get('traffic_mode') === 'default') config.traffic = structuredClone(trafficDefaults);
  else if (values.has('traffic_mode')) {
    config.traffic = structuredClone(source.traffic || trafficDefaults);
    for (const name of Object.keys(trafficDefaults)) {
      if (name === 'mix') {
        for (const kind of Object.keys(trafficDefaults.mix)) {
          if (!values.has(`traffic_mix_${kind}`)) continue;
          config.traffic.mix[kind] = numeric(values.get(`traffic_mix_${kind}`), `${kind} weight`, allowInvalid);
        }
      } else if (!values.has(`traffic_${name}`)) continue;
      else if (name === 'rates') {
        const parts = String(values.get('traffic_rates') || '').trim().split(/[\s,]+/);
        if (parts.some(part => !part) && !allowInvalid) throw new Error('Enter at least one arrival rate.');
        config.traffic.rates = parts.filter(Boolean).map(part => numeric(part, 'Arrival rate', allowInvalid));
      } else if (name === 'arrival') config.traffic.arrival = values.get('traffic_arrival');
      else {
        const value = numeric(values.get(`traffic_${name}`), name, allowInvalid);
        if (name === 'max_in_flight' && source.traffic?.max_in_flight == null &&
            value === inheritedConcurrency(config, config.default_concurrency)) {
          if (Object.hasOwn(source.traffic || {}, name)) config.traffic[name] = null;
          else delete config.traffic[name];
        } else config.traffic[name] = value;
      }
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

export function configureSessions(source, values, defaults, {allowInvalid=false}={}) {
  const config = structuredClone(source);
  if (!values.has('sessions_profile')) return config;
  if (values.get('sessions_profile') === 'correctness' && !config.sessions) return config;
  const settings = config.sessions ||= {...structuredClone(defaults),
    max_tokens:Math.min(defaults.max_tokens,config.max_output_tokens)};
  settings.profile = values.get('sessions_profile');
  if (settings.profile !== 'performance') return config;
  settings.modes = ['live'];
  settings.sessions = numeric(values.get('sessions_count'), 'Total conversations', allowInvalid);
  settings.delay_seconds = numeric(values.get('sessions_delay'), 'Pause between turns', allowInvalid);
  if (values.has('profile_sessions_concurrency')) {
    const value = numeric(values.get('profile_sessions_concurrency'), 'Concurrent conversations', allowInvalid);
    const edited = JSON.parse(values.get('profile_edits_sessions') || '[]').includes('concurrency');
    if (edited || Object.hasOwn(source.sessions || {}, 'concurrency') || value !== inheritedConcurrency(config, defaults.concurrency)) settings.concurrency = value;
    else delete settings.concurrency;
  }
  settings.max_tokens = numeric(values.get(values.has('profile_sessions_max_tokens') ? 'profile_sessions_max_tokens' : 'sessions_max_tokens'), 'Conversation output cap', allowInvalid);
  settings.workloads = [];
  for (let i=0;i<Number(values.get('session_workload_count'));i++) {
    if (values.get(`session_enabled_${i}`) !== 'on') continue;
    const previous = source.sessions?.workloads?.[i] || {};
    settings.workloads.push({...previous,
      name: String(values.get(`session_name_${i}`) || '').trim(),
      weight: numeric(values.get(`session_weight_${i}`), 'Conversation weight', allowInvalid),
      max_tokens: numeric(values.get(`session_cap_${i}`), 'Task output cap', allowInvalid),
      turns: Array.from({length:Number(values.get(`session_turn_count_${i}`))}, (_, turn) =>
        String(values.get(`session_turn_${i}_${turn}`) || '')),
    });
  }
  if (!settings.workloads.length && !allowInvalid) throw new Error('Select at least one conversation task.');
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
    profile.max_tokens = Math.min(profile.max_tokens, config.max_output_tokens);
    profile.concurrency = inheritedConcurrency(config, Math.min(profile.concurrency, profile.sessions));
  }
  if (key === 'consistency') profile.levels = [...new Set(profile.levels.map(level => inheritedConcurrency(config, level)))].sort((left,right) => left-right);
  if (key === 'arrivals') profile.max_in_flight = inheritedConcurrency(config, config.default_concurrency ?? profile.max_in_flight);
  return profile;
}

export function parseLogprobSettings(raw) {
  let value;
  try { value = JSON.parse(raw); } catch { throw new Error('Logprob settings must be a JSON object or null.'); }
  if (value !== null && (typeof value !== 'object' || Array.isArray(value))) throw new Error('Logprob settings must be a JSON object or null.');
  return value;
}

export function parseToolFixtures(raw, limits={}) {
  const maximum = limits['tool_checks.bytes']?.maximum;
  if (maximum != null && new TextEncoder().encode(raw).length > maximum) throw new Error(`Tool fixtures must fit within ${maximum.toLocaleString('en-US')} bytes.`);
  let fixtures;
  try { fixtures = JSON.parse(raw); }
  catch { throw new Error('Tool fixtures must be a JSON array. Use [] for an empty list.'); }
  if (!Array.isArray(fixtures) || fixtures.some(item => !item || typeof item !== 'object' || Array.isArray(item))) {
    throw new Error('Tool fixtures must be a JSON array of fixture objects.');
  }
  const maxItems = limits['tool_checks.fixtures']?.maxItems;
  if (maxItems != null && fixtures.length > maxItems) throw new Error(`Configure at most ${maxItems} tool fixtures.`);
  return fixtures;
}

// Catalog profile forms patch only fields the operator edited. Viewing a null
// profile or filtering a check must not materialize defaults or erase JSON data.
export function configureProfiles(source, values, defaults, {allowInvalid=false, limits={}}={}) {
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
          try { profile[name] = parseToolFixtures(raw, limits); }
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
    const inheritedField = {arrivals:'max_in_flight',sessions:'concurrency',consistency:'levels'}[key];
    if (inheritedField && !edits.includes(inheritedField) && !Object.hasOwn(config[key] || {}, inheritedField)) {
      delete profile[inheritedField];
    }
    config[key] = profile;
  }
  if (!allowInvalid && typeof config.consistency?.logprobs === 'string') config.consistency.logprobs = parseLogprobSettings(config.consistency.logprobs);
  for (const name of ['fixtures', 'conversations']) {
    if (!allowInvalid && typeof config.tool_checks?.[name] === 'string') {
      config.tool_checks[name] = parseToolFixtures(config.tool_checks[name], limits);
    }
  }
  if (!allowInvalid && config.tool_calling && config.tool_checks?.modes?.includes('stream') && !config.stream) {
    throw Object.assign(new Error('Enable streaming on Connect or choose complete tool requests.'), {
      errors:[{fields:['tool_checks.modes','stream'],msg:'Enable streaming on Connect or choose complete tool requests.'}],
    });
  }
  return config;
}

// HTML has no exclusive min attribute. Apply the same schema rule to positive
// numeric fields and array entries before preview/validation makes an API call.
export function positiveBoundErrors(config, limits={}, explanations={}) {
  const errors=[];
  function walk(value,path,parent=null) {
    const canonical=path.replace(/\.\d+(?=\.|$)/g,'[]');
    const bound=limits[canonical]||{},policy=explanations[canonical]||{};
    if(typeof value==='number'&&Number.isFinite(value)&&bound.exclusiveMinimum!=null&&value<=bound.exclusiveMinimum){
      const field=/\.\d+$/.test(path)?path.replace(/\.\d+$/,''):path;
      const arrayItem=Array.isArray(parent),parentPolicy=explanations[canonical.replace(/\[\]$/,'')];
      errors.push({fields:[field],values:{[field]:arrayItem?parent:value},msg:(arrayItem?parentPolicy?.errors?.item_exclusiveMinimum:null)||policy.errors?.exclusiveMinimum||`${policy.label||path} must be greater than ${bound.exclusiveMinimum}.`});
    }
    if(Array.isArray(value))value.forEach((item,index)=>walk(item,`${path}.${index}`,value));
    else if(value&&typeof value==='object')for(const [key,item] of Object.entries(value))walk(item,path?`${path}.${key}`:key);
  }
  walk(config,'');return errors;
}
