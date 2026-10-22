"""New recoverable rejection must be proven by this exact local operation."""
from copy import deepcopy
import json
import pytest
from shengbo_contracts.business import COMMAND_ARGS
from shengbo_contracts.common import ContractError, canonical_hash, SCHEMA_VERSION
from shengbo_prelive.middleware import ActionBoundary
from shengbo_prelive.models import RunScope
from test_middleware import FakeEngine, tool_request

CODE='BASE_MODULE_TARGET_MISMATCH'
SECRET='untrusted-upstream-message-do-not-copy'

def ref(kind,identity):
 return {'kind':kind,'id':identity,'revision':1,'content_hash':canonical_hash(identity)}

def case():
 engine=FakeEngine()
 product=ref('product','product-one'); bundle=ref('context','bundle-one'); module=ref('module','partial-opening')
 args={'preparation_id':'prep-one','expected_revision':1,'bundle_ref':bundle,'items':[
  {'product_ref':product,'variant_id':'v1_commute','version_number':1,'modules':[
   {'key':'opening_long','action':'reuse','base_module_ref':module},
   *[{'key':key,'action':'generate'} for key in ['product_intro','selling_points','cta']]]}]}
 req=tool_request('save_script_change_plan',args=args)
 typed=COMMAND_ARGS['save_script_plan'].model_validate(args).model_dump(mode='json',exclude_none=True)
 op_id='op_'+canonical_hash(['r1',req.tool_call['id'],'save_script_plan'])[7:39]
 op={'id':op_id,'run_id':'r1','key':'save_script_plan','status':'known_failed','error_code':CODE,
     'args':typed,'request_hash':canonical_hash(typed)}
 engine.store.data[('operation',op_id)]=op
 details={'command':'save_script_plan','commit_state':'not_committed','preparation_ref':ref('preparation','prep-one'),
  'bundle_ref':bundle,'item_index':0,'module_key':'opening_long','requested_product_ref':product,
  'requested_variant_id':'v1_commute','requested_version_number':1,'requested_base_module_ref':module,
  'requested_base_script_ref':None,'reason':'BASE_SCRIPT_REQUIRED_FOR_EXPLICIT_MODULE_REUSE'}
 error=ContractError(CODE,SECRET,status=422,details=deepcopy(details))
 error.known_operation_id=op_id;error.known_operation_input_hash=canonical_hash(typed)
 return engine,req,error,op

@pytest.mark.asyncio
async def test_attested_rejection_is_safe_bounded_feedback_without_automatic_retry():
 engine,req,error,op=case(); before=deepcopy(op); invoked=[]
 async def reject(request):
  invoked.append(request.tool_call['id']); raise error
 for number in range(1,4):
  response=await ActionBoundary(engine).awrap_tool_call(req,reject)
  body=json.loads(response.content)
  assert response.status=='error' and body['correction_count']==number
  assert body['error']['details']['reason']=='BASE_SCRIPT_REQUIRED_FOR_EXPLICIT_MODULE_REUSE'
  assert body['recovery']['automatic_action_taken'] is False
  assert body['recovery']['explicit_model_action_required'] is True
  assert SECRET not in response.content
 with pytest.raises(ContractError):
  await ActionBoundary(engine).awrap_tool_call(req,reject)
 assert len(invoked)==4 and op==before
 assert engine.store.get('run','r1')['correctable_tool_errors']=={CODE:3}

@pytest.mark.asyncio
@pytest.mark.parametrize('canonicalized',['preparation_ref','bundle_ref','requested_product_ref','requested_base_module_ref'])
async def test_valid_record_to_artifact_representation_keeps_exact_identity(canonicalized):
 engine,req,error,op=case()
 error.details[canonicalized]['schema_version']=SCHEMA_VERSION
 async def reject(_): raise error
 response=await ActionBoundary(engine).awrap_tool_call(req,reject)
 body=json.loads(response.content)
 assert response.status=='error' and body['correction_count']==1
 assert body['error']['details'][canonicalized]['content_hash']==error.details[canonicalized]['content_hash']

@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',[
 'not_attested','wrong_id','wrong_tool','child','intent','status409','status403','status503','unknown',
 'op_run','op_key','op_status','op_error','op_hash','op_input','wire_extra','wrong_prep','wrong_revision',
 'wrong_bundle','wrong_index','wrong_product','wrong_variant','wrong_version','wrong_module','wrong_key',
 'has_base','changed_request','wrong_input_attest','bundle_hash','module_hash','module_revision',
 'unsupported_schema','request_unsupported_schema'])
async def test_no_forged_or_unknown_rejection_can_reenter_planning(mutation):
 engine,req,error,op=case()
 if mutation=='not_attested': del error.known_operation_id
 elif mutation=='wrong_id': req.tool_call['id']='other-call'
 elif mutation=='wrong_tool': req.tool_call['name']='execute_script_plan'
 elif mutation=='child': req.runtime.context=RunScope('r1','invoke','execute','product_facts','b1')
 elif mutation=='intent': req.runtime.context=RunScope('r1','invoke','intent')
 elif mutation.startswith('status'): error.status=int(mutation[6:])
 elif mutation=='unknown': engine.store.update('run','r1',pending_unknown_operation='unsettled')
 elif mutation=='op_run': op['run_id']='other-run'
 elif mutation=='op_key': op['key']='save_product'
 elif mutation=='op_status': op['status']='sent'
 elif mutation=='op_error': op['error_code']='OTHER'
 elif mutation=='op_hash': op['request_hash']='invalid'
 elif mutation=='op_input':
  op['args']['items'][0]['version_number']=2;op['request_hash']=canonical_hash(op['args'])
 elif mutation=='wire_extra': error.details['input']=SECRET
 elif mutation=='wrong_prep': error.details['preparation_ref']['id']='foreign-prep'
 elif mutation=='wrong_revision': error.details['preparation_ref']['revision']=2
 elif mutation=='wrong_bundle': error.details['bundle_ref']['id']='foreign-context'
 elif mutation=='wrong_index': error.details['item_index']=1
 elif mutation=='wrong_product': error.details['requested_product_ref']['id']='foreign-product'
 elif mutation=='wrong_variant': error.details['requested_variant_id']='other-variant'
 elif mutation=='wrong_version': error.details['requested_version_number']=2
 elif mutation=='wrong_module': error.details['requested_base_module_ref']['id']='foreign-module'
 elif mutation=='wrong_key': error.details['module_key']='opening_short'
 elif mutation=='has_base': req.tool_call['args']['items'][0]['base_script_ref']=ref('script_revision','invented')
 elif mutation=='changed_request': req.tool_call['args']['items'][0]['variant_id']='other'
 elif mutation=='wrong_input_attest': error.known_operation_input_hash=canonical_hash('other')
 elif mutation=='bundle_hash': error.details['bundle_ref']['content_hash']=canonical_hash('other')
 elif mutation=='module_hash': error.details['requested_base_module_ref']['content_hash']=canonical_hash('other')
 elif mutation=='module_revision': error.details['requested_base_module_ref']['revision']=2
 elif mutation=='unsupported_schema': error.details['requested_base_module_ref']['schema_version']='OTHER'
 elif mutation=='request_unsupported_schema':
  req.tool_call['args']['items'][0]['modules'][0]['base_module_ref']['schema_version']='OTHER'
  typed=COMMAND_ARGS['save_script_plan'].model_validate(req.tool_call['args']).model_dump(mode='json',exclude_none=True)
  op.update(args=typed,request_hash=canonical_hash(typed))
  error.known_operation_input_hash=canonical_hash(typed)
 async def reject(_): raise error
 with pytest.raises(ContractError):
  await ActionBoundary(engine).awrap_tool_call(req,reject)
 assert not engine.store.get('run','r1').get('correctable_tool_errors')
