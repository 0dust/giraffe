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

export function inferSetupPreset(config) {
  const ids=config.checks||[];
  if(ids.length===2&&ids.includes('access')&&ids.includes('correctness'))return 'answers';
  if(ids.length===2&&ids.includes('access')&&ids.includes('load'))return 'concurrent';
  if(ids.length===3&&['access','correctness','load'].every(id=>ids.includes(id)))return 'arrival';
  return 'custom';
}
export function applySetupPreset(source,preset,defaults) {
  const config=structuredClone(source),selection={answers:['access','correctness'],concurrent:['access','load'],arrival:['access','correctness','load']}[preset];
  if(selection)config.checks=selection;
  config.structured_json=config.checks.includes('json');config.metrics=config.checks.includes('gpu');config.tool_calling=config.checks.includes('tools');
  return config;
}
export function configureSetupLoad(source) {return structuredClone(source);}

export function loadTestSettings(config, defaults) {
  const merge=(base, extra)=>{
    const result=structuredClone(base);
    for(const [key,value] of Object.entries(extra || {}))result[key]=value&&typeof value==='object'&&!Array.isArray(value)&&result[key]&&typeof result[key]==='object'?merge(result[key],value):structuredClone(value);
    return result;
  };
  const settings=merge(defaults,config.load_test);
  if(config.load_test?.patterns?.concurrent?.levels===undefined){
    const ceiling=config.concurrency==null?Infinity:Math.max(1,Math.floor(config.concurrency/(config.overlap_models?config.targets?.length||1:1)));
    settings.patterns.concurrent.levels=defaults.patterns.concurrent.levels.filter(level=>level<=ceiling);
    if(!settings.patterns.concurrent.levels.length)settings.patterns.concurrent.levels=[1];
  }
  return settings;
}

export const loadFieldName=path=>'load_'+path.replace(/^load_test\./,'').replaceAll('.','_');
const loadSizes=['short','medium','long'];
const loadShapes=loadSizes.flatMap(input=>loadSizes.map(output=>`${input}_${output}`));

export function loadTestIssues(config, defaults, limits={}) {
  if(!config.checks?.includes('load'))return [];
  const s=loadTestSettings(config,defaults),w=s.workloads,p=s.patterns,issues=[];
  const issue=(path,msg)=>issues.push({fields:[path.startsWith('load_test.')||['concurrency','max_requests','max_duration_seconds'].includes(path)?path:'load_test.'+path],msg});
  const valid=(path,value,min,{integer=false,max=Infinity,label=path,exclusive=false}={})=>{
    const bounds=limits['load_test.'+path]||{};
    const maximum=Math.min(max,bounds.maximum??Infinity);
    if(typeof value!=='number'||!Number.isFinite(value)||value<min||value>maximum||(exclusive&&value===min)||(integer&&!Number.isInteger(value))){
      const message=path==='workloads.interference.max_slowdown'?'Enter a slowdown greater than 1×; 1.5× means up to 50% longer.':`Enter ${integer?'a whole number':'a number'} ${exclusive?'greater than ':'of at least '}${min}${Number.isFinite(maximum)?` and no more than ${maximum}`:''} for ${label}.`;
      issue(path,message);
    }
  };
  for(const kind of ['input_tokens','output_tokens'])for(let i=0;i<3;i++){
    const value=s.profile[loadSizes[i]][kind],path=`profile.${loadSizes[i]}.${kind}`,bounds=limits['load_test.'+path]||{};
    if(!Number.isInteger(value)||value<1)issue(path,'Token sizes must be positive whole numbers.');
    else if(value>(bounds.maximum??Infinity))issue(path,`Token sizes must not exceed ${bounds.maximum}.`);
    if(i&&value<=s.profile[loadSizes[i-1]][kind])issue(path,'Use increasing Short, Medium and Long token sizes.');
  }
  if(w.combinations.enabled&&!w.combinations.selected.length)issue('workloads.combinations.selected','Select at least one size combination.');
  if(w.mixed.enabled){
    if(w.mixed.selected.length<2)issue('workloads.mixed.selected','Select at least two request types for mixed traffic.');
    if(w.mixed.share_mode==='custom'){
      const shares=w.mixed.selected.map(shape=>w.mixed.shares[shape]),total=shares.reduce((sum,n)=>sum+Number(n),0);
      if(!Number.isFinite(total)||Math.abs(total-100)>0.001||shares.some(n=>typeof n!=='number'||n<=0||n>100)){
        const bad=w.mixed.selected.find(shape=>typeof w.mixed.shares[shape]!=='number'||!Number.isFinite(w.mixed.shares[shape])||w.mixed.shares[shape]<=0||w.mixed.shares[shape]>100);
        issue(bad?'workloads.mixed.shares.'+bad:'workloads.mixed.share_mode','Give each selected mix type a positive share, totaling 100%.');
      }
    }
  }
  if(w.stress.enabled&&!w.stress.cases.length)issue('workloads.stress.cases','Select at least one token-stress case.');
  if(!Object.values(w).some(row=>row.enabled))issue('workloads.combinations.enabled','Select at least one workload.');
  const standard=(w.combinations.enabled?w.combinations.selected.length:0)+(w.mixed.enabled?1:0)+(w.stress.enabled?w.stress.cases.length:0);
  if(standard&&!Object.values(p).some(row=>row?.enabled))issue('patterns.poisson.enabled','Select a traffic pattern for the selected workloads.');
  const levels=p.concurrent.levels,rates=p.rates;
  const list=(path,values,integer)=>{if(!Array.isArray(values)||!values.length||values.length>(limits['load_test.'+path]?.maxItems??Infinity)||values.some((n,i)=>typeof n!=='number'||!Number.isFinite(n)||n<=0||(integer&&!Number.isInteger(n))||(i&&n<=values[i-1])||n>(limits['load_test.'+path]?.items?.maximum??Infinity)))issue(path,'Enter positive, increasing '+(integer?'whole-number levels':'rates')+' separated by commas.');};
  if(standard&&p.concurrent.enabled||w.interference.enabled)list('patterns.concurrent.levels',levels,true);
  if(standard&&(p.constant.enabled||p.poisson.enabled))list('patterns.rates',rates,false);
  if(standard||w.interference.enabled)valid('schedule.duration_seconds',s.schedule.duration_seconds,1,{integer:true,label:'Send new requests for (seconds)'});
  if(standard&&p.poisson.enabled)valid('patterns.poisson.seed',p.poisson.seed,0,{integer:true,label:'Random seed'});
  if(standard&&p.bursts.enabled){
    valid('patterns.bursts.size',p.bursts.size,1,{integer:true,label:'Requests per burst'});
    valid('patterns.bursts.first_seconds',p.bursts.first_seconds,0,{label:'First burst after (seconds)'});
    valid('patterns.bursts.gap_seconds',p.bursts.gap_seconds,0,{exclusive:true,label:'Time between burst starts (seconds)'});
    if(p.bursts.first_seconds>=s.schedule.duration_seconds)issue('patterns.bursts.first_seconds','The first burst must start before the sending window ends.');
    if(config.concurrency!=null&&p.bursts.size>config.concurrency)issue('concurrency','One burst exceeds the concurrency ceiling and cannot be dispatched in full.');
  }
  if((standard&&p.concurrent.enabled||w.interference.enabled)&&config.concurrency!=null&&levels.some(n=>n>config.concurrency))issue('concurrency','Concurrency levels exceed the run ceiling. Update Run limits or lower the levels.');
  if(w.interference.enabled){
    if(!levels.some(n=>Number.isInteger(n)&&n>=2))issue('patterns.concurrent.levels','The long-input / short-answer test needs a concurrency level of at least 2.');
    valid('workloads.interference.max_slowdown',w.interference.max_slowdown,1,{exclusive:true,label:'Maximum allowed slowdown of short answers (×)'});
  }
  if(w.stress.enabled&&w.stress.cases.some(shape=>shape.endsWith('_long'))){
    valid('workloads.stress.min_output_percent',w.stress.min_output_percent,1,{label:'Minimum generated output (% of long-output cap)'});
    if(w.stress.min_output_percent>100)issue('workloads.stress.min_output_percent','Output coverage cannot exceed 100% of the cap.');
  }
  valid('request_timeout_seconds',s.request_timeout_seconds,0,{exclusive:true,label:'Request timeout (seconds)'});
  valid('pass_criteria.min_valid_responses',s.pass_criteria.min_valid_responses,1,{integer:true,label:'Minimum valid responses per result group'});
  for(const [key,label] of [['max_error_percent','Errors allowed (%)'],['min_correct_percent','Correct answers required (%)']]){
    valid('pass_criteria.'+key,s.pass_criteria[key],0,{label});
    if(s.pass_criteria[key]>100)issue('pass_criteria.'+key,'Percentages must be from 0 to 100.');
  }
  for(const [key,label] of [['max_first_answer_ms','Maximum time to first answer per response (ms)'],['max_completion_ms','Maximum completion time per response (ms)'],['max_stream_pause_ms','Maximum pause between streamed chunks (ms)'],['min_generation_tokens_per_second','Minimum generation speed per response (tokens/sec)']])if(s.pass_criteria[key]!=null)valid('pass_criteria.'+key,s.pass_criteria[key],0,{exclusive:true,label});
  return issues;
}

export function configureLoadTest(source, values, defaults, {allowInvalid=false,limits={}}={}) {
  const config=structuredClone(source);
  if(!values.has('load_rendered'))return config;
  const selectedCheck=config.checks?.includes('load');
  const settings=loadTestSettings(source,defaults),before=structuredClone(settings),w=settings.workloads,p=settings.patterns;
  const checkbox=(path,object,key)=>{const name=loadFieldName(path);object[key]=values.get(name)==='on';};
  for(const kind of ['combinations','mixed','interference','stress'])checkbox(`workloads.${kind}.enabled`,w[kind],'enabled');
  for(const kind of ['concurrent','constant','poisson','bursts'])checkbox(`patterns.${kind}.enabled`,p[kind],'enabled');
  for(const kind of ['combinations','mixed']){const selected=loadShapes.filter(shape=>values.get(`load_workloads_${kind}_selected_${shape}`)==='on');w[kind].selected=[...w[kind].selected.filter(shape=>selected.includes(shape)),...selected.filter(shape=>!w[kind].selected.includes(shape))];}
  w.stress.cases=['long_short','short_long','long_long'].filter(shape=>values.get(`load_workloads_stress_cases_${shape}`)==='on');
  for(const [path,object,key] of [['workloads.mixed.share_mode',w.mixed,'share_mode'],['workloads.interference.trigger',w.interference,'trigger']])if(values.has(loadFieldName(path)))object[key]=values.get(loadFieldName(path));
  const standard=w.combinations.enabled||w.mixed.enabled||w.stress.enabled;
  const set=(path,object,key,active=true,nullable=false)=>{
    const name=loadFieldName(path);if(!values.has(name))return;
    let candidate=numeric(values.get(name),path,true);
    if(candidate===null&&!nullable)candidate='';
    if(!active||!selectedCheck){
      const bounds=limits['load_test.'+path]||limits['load_test.workloads.mixed.shares.*']||{};
      if(typeof candidate!=='number'||!Number.isFinite(candidate)||candidate<(bounds.minimum??-Infinity)||candidate>(bounds.maximum??Infinity)||(bounds.exclusiveMinimum!=null&&candidate<=bounds.exclusiveMinimum)||(bounds.type==='integer'&&!Number.isInteger(candidate)))return;
    }
    object[key]=candidate;
  };
  for(const size of loadSizes)for(const key of ['input_tokens','output_tokens'])set(`profile.${size}.${key}`,settings.profile[size],key);
  for(const shape of loadShapes)set('workloads.mixed.shares.'+shape,w.mixed.shares,shape,w.mixed.enabled&&w.mixed.share_mode==='custom'&&w.mixed.selected.includes(shape));
  set('workloads.interference.max_slowdown',w.interference,'max_slowdown',w.interference.enabled);
  set('workloads.stress.min_output_percent',w.stress,'min_output_percent',w.stress.enabled&&w.stress.cases.some(shape=>shape.endsWith('_long')));
  for(const [path,object,key,active] of [['patterns.concurrent.levels',p.concurrent,'levels',standard&&p.concurrent.enabled||w.interference.enabled],['patterns.rates',p,'rates',standard&&(p.constant.enabled||p.poisson.enabled)]]){
    if(active&&values.has(loadFieldName(path)))object[key]=String(values.get(loadFieldName(path))).split(',').map(n=>n.trim()===''?'':numeric(n,path,true));
  }
  set('patterns.poisson.seed',p.poisson,'seed',standard&&p.poisson.enabled);
  for(const key of ['size','first_seconds','gap_seconds'])set('patterns.bursts.'+key,p.bursts,key,standard&&p.bursts.enabled);
  set('schedule.duration_seconds',settings.schedule,'duration_seconds',standard||w.interference.enabled);
  set('request_timeout_seconds',settings,'request_timeout_seconds');
  for(const key of Object.keys(settings.pass_criteria))set('pass_criteria.'+key,settings.pass_criteria,key,true,!['max_error_percent','min_correct_percent','min_valid_responses'].includes(key));
  config.load_test=settings;
  if(!selectedCheck&&loadTestIssues({...config,checks:['load']},defaults,limits).length)return structuredClone(source);
  if(!allowInvalid){const errors=loadTestIssues(config,defaults,limits);if(errors.length)throw Object.assign(new Error(errors[0].msg),{errors});}
  // Opening the drawer and Reset must preserve implicit inheritance and the
  // portable config, rather than materializing a block of form defaults.
  if(sameConfig(before,settings))return structuredClone(source);
  if(source.load_test?.patterns?.concurrent?.levels===undefined&&sameConfig(before.patterns.concurrent.levels,settings.patterns.concurrent.levels))delete settings.patterns.concurrent.levels;
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
  const pauseMode=values.get('session_pause_mode');
  if (values.get('sessions_profile') === 'correctness' && !config.sessions &&
      pauseMode !== 'dynamic' && (pauseMode === 'fixed' || (
        !String(values.get('session_sample_think_time_seconds') || '').trim() &&
        !String(values.get('session_weights_think_time_seconds') || '').trim()))) return config;
  const settings = config.sessions ||= {...structuredClone(defaults),
    max_tokens:Math.min(defaults.max_tokens,config.max_output_tokens)};
  settings.profile = values.get('sessions_profile');
  if (values.has('session_sampling_seed')) {
    const sampling={};
    for (const key of ['think_time_seconds','input_tokens','output_tokens','turn_count']) {
      if (settings.profile !== 'performance' && key !== 'think_time_seconds') continue;
      if (key === 'think_time_seconds' && pauseMode === 'fixed') continue;
      const text=String(values.get(`session_sample_${key}`)||'').trim();
      const weights=String(values.get(`session_weights_${key}`)||'').trim();
      if (key === 'think_time_seconds' && pauseMode === 'dynamic' && !text && !allowInvalid) {
        throw Object.assign(new Error('Enter pause values for dynamic pauses.'), {
          errors:[{fields:['sessions.sampling.think_time_seconds.values'],msg:'Enter pause values for dynamic pauses.'}],
        });
      }
      if (key === 'think_time_seconds' && pauseMode === 'dynamic' && !text) {sampling[key]={values:[]};continue;}
      if (!text && !weights) continue;
      const parse=(value,label)=>value.split(',').map(item=>{
        if (!item.trim() && !allowInvalid) throw new Error(`${label}: enter comma-separated numbers.`);
        return numeric(item.trim(),label,allowInvalid);
      });
      sampling[key]={values:text?parse(text,key):[]};
      if(weights)sampling[key].weights=parse(weights,`${key} weights`);
    }
    settings.sampling=Object.keys(sampling).length?{seed:numeric(values.get('session_sampling_seed'), 'Sampling seed', allowInvalid),...sampling}:null;
  }
  if (settings.profile !== 'performance') return config;
  settings.modes = ['live'];
  settings.sessions = numeric(values.get('sessions_count'), 'Total conversations', allowInvalid);
  if (pauseMode !== 'dynamic') settings.delay_seconds = numeric(values.get(values.has('profile_sessions_delay_seconds') ? 'profile_sessions_delay_seconds' : 'sessions_delay'), 'Pause between turns', allowInvalid);
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
  for (const key of ['prefix', 'sessions', 'consistency', 'tools']) {
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
      if (key === 'sessions' && name === 'delay_seconds' && values.get('session_pause_mode') === 'dynamic') continue;
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
