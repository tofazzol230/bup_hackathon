import json
import sys
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
TOLERANCE = 0.02

def check_physics(inp, result):
    battery = inp['battery']
    hours_data = inp['hours']
    directives = result['directive_interpretation']
    plan = sorted(result['hourly_plan'], key=lambda e: e['hour'])
    capacity = float(battery['capacity_kwh'])
    initial = float(battery['initial_energy_kwh'])
    base_min = float(battery['minimum_energy_kwh'])
    max_c = float(battery['max_charge_kwh_per_hour'])
    max_d = float(battery['max_discharge_kwh_per_hour'])
    eff_solar = [float(h['solar_kwh']) for h in hours_data]
    for d in directives:
        if d.get('applies') and d.get('directive_type') == 'solar_reduction':
            adj = d.get('structured_adjustment') or {}
            f = float(adj.get('factor', 1.0))
            for hr in adj.get('hours', []):
                if 0 <= hr < 24: eff_solar[hr] *= f
    min_res = [base_min] * 24
    for d in directives:
        if d.get('applies') and d.get('directive_type') == 'minimum_battery_reserve':
            adj = d.get('structured_adjustment') or {}
            v = float(adj.get('minimum_energy_kwh', base_min))
            for hr in adj.get('hours', []):
                if 0 <= hr < 24: min_res[hr] = max(min_res[hr], v)
    no_c = set()
    for d in directives:
        if d.get('applies') and d.get('directive_type') == 'no_charge_window':
            for hr in (d.get('structured_adjustment') or {}).get('hours', []):
                if 0 <= hr < 24: no_c.add(hr)
    no_d = set()
    for d in directives:
        if d.get('applies') and d.get('directive_type') == 'no_discharge_window':
            for hr in (d.get('structured_adjustment') or {}).get('hours', []):
                if 0 <= hr < 24: no_d.add(hr)
    max_g = [float('inf')] * 24
    for d in directives:
        if d.get('applies') and d.get('directive_type') == 'max_grid_window':
            adj = d.get('structured_adjustment') or {}
            cap = float(adj.get('max_grid_kwh', float('inf')))
            for hr in adj.get('hours', []):
                if 0 <= hr < 24: max_g[hr] = min(max_g[hr], cap)
    errors = []
    rg = rc = rp = 0.0
    prev = initial
    for e in plan:
        h = e['hour']
        g = float(e['grid_kwh'])
        s = float(e['solar_used_kwh'])
        act = e['battery_action']
        bk = float(e['battery_kwh'])
        ea = float(e['battery_energy_after_kwh'])
        dem = float(hours_data[h]['demand_kwh'])
        tar = float(hours_data[h]['tariff_bdt_per_kwh'])
        ch = bk if act == 'charge' else 0.0
        di = bk if act == 'discharge' else 0.0
        bal = g + s + di - ch
        if abs(bal - dem) > TOLERANCE: errors.append(f'H{h} energy balance {bal:.4f}!={dem}')
        if s > eff_solar[h] + TOLERANCE: errors.append(f'H{h} solar {s}>eff {eff_solar[h]:.4f}')
        exp_ea = prev + ch - di
        if abs(ea - exp_ea) > TOLERANCE: errors.append(f'H{h} batt transition {exp_ea:.4f}!={ea}')
        if ea < min_res[h] - TOLERANCE: errors.append(f'H{h} batt {ea}<reserve {min_res[h]}')
        if ea > capacity + TOLERANCE: errors.append(f'H{h} batt {ea}>cap {capacity}')
        if ch > max_c + TOLERANCE: errors.append(f'H{h} charge {ch}>max {max_c}')
        if h in no_c and ch > TOLERANCE: errors.append(f'H{h} charge in no_charge_window')
        if di > max_d + TOLERANCE: errors.append(f'H{h} discharge {di}>max {max_d}')
        if h in no_d and di > TOLERANCE: errors.append(f'H{h} discharge in no_discharge_window')
        if g > max_g[h] + TOLERANCE: errors.append(f'H{h} grid {g}>cap {max_g[h]}')
        rg += g; rc += g * tar
        if g > rp: rp = g
        prev = ea
    if abs(prev - initial) > TOLERANCE: errors.append(f'neutrality {prev:.4f}!={initial}')
    if abs(float(result['total_grid_kwh']) - rg) > TOLERANCE: errors.append(f'total_grid {result["total_grid_kwh"]}!={rg:.4f}')
    if abs(float(result['total_cost_bdt']) - rc) > TOLERANCE: errors.append(f'total_cost {result["total_cost_bdt"]}!={rc:.4f}')
    if abs(float(result['peak_grid_kwh']) - rp) > TOLERANCE: errors.append(f'peak_grid {result["peak_grid_kwh"]}!={rp:.4f}')
    return errors

def run_sample(case):
    cid = case['id']
    nm = case.get('name') or case.get('label', cid)
    inp = case['input']
    exp = case['expected_output']
    exp_cost = float(exp['total_cost_bdt'])
    print(f'--- Running {cid}: {nm} ---')
    resp = client.post('/optimize-energy', json=inp)
    if resp.status_code != 200:
        print(f'FAIL: {cid} HTTP {resp.status_code}: {resp.text[:200]}')
        return False
    result = resp.json()
    errors = []
    if result.get('scenario_id') != inp.get('scenario_id'): errors.append(f'scenario_id mismatch')
    di = result.get('directive_interpretation', [])
    n = len(inp['operator_notes'])
    if len(di) != n: errors.append(f'directive count {len(di)}!={n}')
    for idx, d in enumerate(di):
        if d.get('note_index') != idx: errors.append(f'note_index[{idx}]={d.get("note_index")}')
    for d in di:
        if d.get('directive_type') == 'no_op':
            if d.get('applies') is not False: errors.append('no_op applies=True')
            if d.get('structured_adjustment') is not None: errors.append('no_op adj not null')
    hp = result.get('hourly_plan', [])
    if len(hp) != 24: errors.append(f'plan length {len(hp)}')
    if sorted([e['hour'] for e in hp]) != list(range(24)): errors.append('hours not 0..23')
    cost_diff = abs(result.get('total_cost_bdt', -1) - exp_cost)
    got_t = [d.get('directive_type') for d in di]
    exp_t = [d.get('directive_type') for d in exp['directive_interpretation']]
    if got_t != exp_t: errors.append(f'types: {got_t} != {exp_t}')
    errors.extend(check_physics(inp, result))
    if errors:
        print(f'FAIL: {cid}')
        for e in errors: print(f'  {e}')
        return False
    print(f'PASS: {cid} (Cost:{result["total_cost_bdt"]:.2f} Diff:{cost_diff:.4f} Physics:21/21 OK)')
    return True

def main():
    with open('BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json', 'r', encoding='utf-8') as f:
        data = json.load(f)
    print('=========================================================')
    r = client.get('/health')
    ok = r.status_code == 200 and r.json().get('status') == 'ok'
    print(f'PASS: /health HTTP 200 {r.json()}' if ok else f'FAIL: /health {r.status_code}')
    print('=========================================================')
    total = len(data['cases'])
    passed = sum(run_sample(c) for c in data['cases'])
    print(f'\nSUMMARY: {passed}/{total} cases passed (21-POINT PHYSICS VALIDATION)!')
    if passed == total:
        print('ALL TESTS PASSED 100%!')
    else:
        print(f'FAILURES: {total - passed}')
        sys.exit(1)

if __name__ == '__main__':
    main()
