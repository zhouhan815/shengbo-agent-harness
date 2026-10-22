"""Actual job-view wiring shares one existing snapshot and grants no action."""
from copy import deepcopy
import pytest
from test_runtime_business import env
from test_recovery_workset_rc11 import make_job, RUN, PREP, CONVERSATION
from shengbo_prelive.snapshot_views import project_snapshot
from shengbo_contracts.common import canonical_hash


@pytest.mark.asyncio
async def test_explicit_job_reader_attaches_exact_partial_readers_without_new_nested_snapshot(env):
 engine,_=env
 run={'id':RUN,'conversation_id':CONVERSATION,'scope':{'kind':'preparation','preparation_id':PREP}}
 job=make_job(); original=deepcopy(job)
 before=len(engine.store.list('context_snapshot'))
 view=engine._job_view(run,job)
 index=view['recovery_workset']
 assert index['status']=='verified_job_snapshot'
 assert len(index['modules'])==3
 # The helper reuses the existing full job snapshot; public response archival
 # may create its own normal receipt, but no helper-specific nested document.
 snapshots=engine.store.list('context_snapshot')
 job_snapshots=[s for s in snapshots if s['body']['document'].get('kind')=='job']
 assert len(job_snapshots)==1
 snapshot=engine.store.get('context_snapshot',view['full_ref']['id'])
 for row in index['modules']:
  if reader:=row.get('accepted_output_reader'):
   assert reader['ref']==view['full_ref']
   document=project_snapshot(snapshot['body']['document'],path=reader['path'])['document']
   assert canonical_hash(document)==row['accepted_output_hash']
 assert not index['approval_implied'] and not index['currentness_verified']
 assert job==original and not engine.store.list('operation')
 # Scheduler/model fixed context does not acquire these additional indices.
 assert 'recovery_workset' not in engine._job_view(run,job,include_provider_outputs=False)


@pytest.mark.asyncio
async def test_other_run_job_does_not_gain_accepted_content_readers(env):
 engine,_=env
 run={'id':'other_run','conversation_id':CONVERSATION,'scope':{'preparation_id':PREP}}
 view=engine._job_view(run,make_job())['recovery_workset']
 assert view['status']=='not_projected' and not view['modules']
 assert view['reason_code']=='JOB_SCOPE_MISMATCH'
