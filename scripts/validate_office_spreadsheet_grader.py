"""Validate original formula conversion/scoring with newly authored toy files."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
import openpyxl
from score_office_spreadsheet import score, verified_module

root = Path('/validation')
root.mkdir(exist_ok=True)
upstream = Path('/opt/grader/upstream')
hashes = {name:hashlib.sha256((upstream/name).read_bytes()).hexdigest()
          for name in ('evaluation/evaluation.py','evaluation/open_spreadsheet.py')}
answers, outputs = [], []
for index, (left,right) in enumerate(((2,3),(7,11),(13,17)),1):
    answer = root / f'{index}_synthetic-only_answer.xlsx'
    candidate = root / f'{index}_synthetic-only_output.xlsx'
    wb = openpyxl.Workbook()
    wb.active.title = 'Data'
    wb.active.append([left,right,left+right])
    wb.save(answer)
    wb.active['C1'] = '=SUM(A1:B1)'
    wb.save(candidate)
    assert openpyxl.load_workbook(candidate,data_only=True).active['C1'].value is None
    answers.append(dict(path=answer.name,role='answer',sha256=hashlib.sha256(answer.read_bytes()).hexdigest()))
    outputs.append(candidate)
manifest = dict(schema='synthetic-validation-only', scoring_source=dict(
    revision='49b73a94775fb489063f60ca1865e3a650079a79',files=hashes),cases=[dict(
    id='synthetic-only',source_id='synthetic-only',instruction_type='Cell-Level Manipulation',
    answer_position='Data!C1',files=answers)])
raw = json.dumps(manifest).encode()
path = root/'manifest.json'
path.write_bytes(raw)
path.with_suffix('.json.sha256').write_text(hashlib.sha256(raw).hexdigest(),encoding='ascii')
recalc = verified_module(upstream,'evaluation/open_spreadsheet.py',hashes['evaluation/open_spreadsheet.py'])
binary = recalc.find_libreoffice()
assert binary, 'LibreOffice executable absent'
cache_probe = root/'recalculation-probe.xlsx'
shutil.copyfile(outputs[0],cache_probe)
assert recalc.just_open_libreoffice(str(cache_probe),binary)
assert openpyxl.load_workbook(cache_probe,data_only=True).active['C1'].value == 5
started = time.perf_counter()
result = score(path,'synthetic-only',upstream,outputs,binary)
assert result['cases']==[True,True,True] and result['soft']==1 and result['hard'] is True
receipt = dict(validation='newly-authored unrelated workbook only',formal_cases_run=0,model_calls=0,
    libreoffice=subprocess.check_output([binary,'--version'],text=True).strip(),
    source_files=hashes,recalculated_cache_value=5,native_synthetic_score=result,
    grading_wall_ms=(time.perf_counter()-started)*1000)
(root/'validation-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf-8')
print(json.dumps(receipt,indent=2))
