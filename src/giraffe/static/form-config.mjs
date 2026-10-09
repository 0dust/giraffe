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
      const value = numeric(values.get(`test_${check.id}_${name}`), name, allowInvalid);
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
  return config;
}
