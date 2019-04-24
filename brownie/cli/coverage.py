#!/usr/bin/python3

from docopt import docopt
from hashlib import sha1
import json
from pathlib import Path
import sys

from brownie.cli.test import get_test_files, run_test
from brownie.test.coverage import merge_coverage
from brownie.cli.utils import color
import brownie.network as network
from brownie.utils.compiler import get_build
import brownie._config as config

CONFIG = config.CONFIG

COVERAGE_COLORS = [
    (0.5, "bright red"),
    (0.85, "bright yellow"),
    (1, "bright green")
]

__doc__ = """Usage: brownie coverage [<filename>] [<range>] [options]

Arguments:
  <filename>          Only run tests from a specific file or folder
  <range>             Number or range of tests to run from file

Options:
  --update            Only evaluate coverage on changed contracts/tests
  --always-transact   Perform all contract calls as transactions
  --verbose           Enable verbose reporting
  --tb                Show entire python traceback on exceptions
  --help              Display this message

Runs unit tests and analyzes the transaction stack traces to estimate
current test coverage. Results are saved to build/coverage.json"""


def main():
    args = docopt(__doc__)

    test_files = get_test_files(args['<filename>'])
    if len(test_files)==1 and args['<range>']:
        try:
            idx = args['<range>']
            if ':' in idx:
                idx = slice(*[int(i)-1 for i in idx.split(':')])
            else:
                idx = slice(int(idx)-1,int(idx))
        except:
            sys.exit("{0[error]}ERROR{0}: Invalid range. Must be an integer or slice (eg. 1:4)".format(color))
    elif args['<range>']:
        sys.exit("{0[error]}ERROR:{0} Cannot specify a range when running multiple tests files.".format(color))
    else:
        idx = slice(0, None)

    network.connect(config.ARGV['network'], True)

    if args['--always-transact']:
        CONFIG['test']['always_transact'] = True
    print("Contract calls will be handled as: {0[value]}{1}{0}".format(
        color,
        "transactions" if CONFIG['test']['always_transact'] else "calls"
    ))

    coverage_files = []

    for filename in test_files:

        coverage_json = Path(CONFIG['folders']['project'])
        coverage_json = coverage_json.joinpath("build/coverage"+filename[5:]+".json")
        coverage_files.append(coverage_json)
        if config.ARGV['update'] and coverage_json.exists():
            continue
        for p in list(coverage_json.parents)[::-1]:
            if not p.exists():
                p.mkdir()

        history, tb = run_test(filename, network, idx)
        if tb:
            if coverage_json.exists():
                coverage_json.unlink()
            continue

        coverage_map = {}
        coverage_eval = {}
        for tx in history:
            if not tx.receiver:
                continue
            for i in range(len(tx.trace)):
                t = tx.trace[i]
                pc = t['pc']
                name = t['contractName']
                source = t['source']['filename']
                if not name or not source:
                    continue
                if name not in coverage_map:
                    coverage_map[name] = get_build(name)['coverageMap']
                    coverage_eval[name] = dict((i,{}) for i in coverage_map[name])
                try:
                    # find the function map item and record the tx
                    
                    fn = next(v for k,v in coverage_map[name][source].items() if pc in v['fn']['pc'])
                    fn['fn'].setdefault('tx',set()).add(tx)
                    if t['op']!="JUMPI":
                        # if not a JUMPI, find the line map item and record
                        ln = next(i for i in fn['line'] if pc in i['pc'])
                        for key in ('tx', 'true', 'false'):
                            ln.setdefault(key, set())
                        ln['tx'].add(tx)
                        continue
                    # if a JUMPI, we need to have hit the jump pc AND a related opcode
                    ln = next(i for i in fn['line'] if pc==i['jump'])
                    for key in ('tx', 'true', 'false'):
                        ln.setdefault(key, set())
                    if tx not in ln['tx']:
                        continue
                    # if the next opcode is not pc+1, the JUMPI was executed truthy
                    key = 'false' if tx.trace[i+1]['pc'] == pc+1 else 'true'
                    ln[key].add(tx)
                # pc didn't exist in map
                except StopIteration:
                    continue

        for contract, source, fn_name, maps in [(k,w,y,z) for k,v in coverage_map.items() for w,x in v.items() for y,z in x.items()]:
            fn = maps['fn']
            if 'tx' not in fn or not fn['tx']:
                coverage_eval[contract][source][fn_name] = {'pct':0}
                continue
            for ln in maps['line']:
                if 'tx' not in ln:
                    ln['count'] = 0
                    continue
                if ln['jump']:
                    ln['jump'] = [len(ln['true']), len(ln['false'])]
                ln['count'] = len(ln['tx'])
            if not [i for i in maps['line'] if i['count']]:
                coverage_eval[contract][source][fn_name] = {'pct':0}
                continue

            count = 0
            coverage = {'line':set(), 'true':set(), 'false':set()}
            for c,i in enumerate(maps['line']):
                if not i['count']:
                    continue
                if not i['jump'] or False not in i['jump']:
                    coverage['line'].add(c)
                    count+=2 if i['jump'] else 1
                    continue
                if i['jump'][0]:
                    coverage['true'].add(c)
                    count+=1
                if i['jump'][1]:
                    coverage['false'].add(c)
                    count+=1
            pct = count / maps['total']
            if count == maps['total']:
                coverage_eval[contract][source][fn_name] = {'pct': 1}
            else:
                coverage['pct']=round(count/maps['total'],2)
                coverage_eval[contract][source][fn_name] = coverage

        build_folder = Path(CONFIG['folders']['project']).joinpath('build/contracts')
        build_files = set(build_folder.joinpath(i+'.json') for i in coverage_eval)
        coverage_eval = {
            'contracts': coverage_eval,
            'sha1': dict((
                str(i),
                # hash of bytecode without final metadata
                sha1(json.load(i.open())['bytecode'][:-68].encode()).hexdigest()
            ) for i in build_files)
        }
        if args['<range>']:
            continue

        test_path = Path(CONFIG['folders']['project']).joinpath(filename+".py")
        coverage_eval['sha1'][str(test_path)] = sha1(test_path.open('rb').read()).hexdigest()

        json.dump(
            coverage_eval,
            coverage_json.open('w'),
            sort_keys=True,
            indent=4,
            default=sorted
        )

    print("\nCoverage analysis complete!\n")
    coverage_eval = merge_coverage(coverage_files)

    for contract in coverage_eval:
        print("  contract: {0[contract]}{1}{0}".format(color, contract))
        for fn_name, pct in [(x,v[x]['pct']) for v in coverage_eval[contract].values() for x in v]:
            c = next(i[1] for i in COVERAGE_COLORS if pct<=i[0])
            print("    {0[contract_method]}{1}{0} - {2}{3:.1%}{0}".format(
                color, fn_name, color(c), pct
            ))
        print()
    print("\nDetailed reports saved in {0[string]}build/coverage{0}".format(color))