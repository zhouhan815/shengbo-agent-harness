/* Browser-only business facade. The server remains the authority. */
(function(global){
  'use strict';
  let sessionPromise;
  const uuid=()=>global.crypto.randomUUID();
  class WorkbenchError extends Error{
    constructor(message,{code='REQUEST_FAILED',details={},status=0,unknown=false,operationId=null}={}){
      super(message);Object.assign(this,{name:'WorkbenchError',code,details,status,unknown,operationId});
    }
  }
  async function session(){
    if(!sessionPromise)sessionPromise=fetch('/api/assistant/v2/session',{credentials:'same-origin',cache:'no-store'}).then(async r=>{
      if(!r.ok)throw new WorkbenchError('无法建立本机操作会话',{status:r.status});
      const value=await r.json();if(typeof value.csrf_token!=='string')throw new WorkbenchError('操作会话缺少校验信息');return value;
    }).catch(error=>{sessionPromise=null;throw error});
    return sessionPromise;
  }
  async function request(path,body,options={}){
    if(typeof path!=='string'||!path.startsWith('/api/')||path.includes('://')||path.includes('..'))throw new WorkbenchError('无效的本机请求地址');
    const authorization=await session();
    const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),options.timeout||45000);
    let response;
    try{
      const method=options.method||(body===undefined?'GET':'POST');
      if(!['GET','POST','PUT','PATCH','DELETE'].includes(method))throw new WorkbenchError('不支持的本机请求方式');
      const extra={};if(options.headers?.['If-Match'])extra['If-Match']=String(options.headers['If-Match']);
      response=await fetch(path,{method,credentials:'same-origin',cache:'no-store',
        headers:body===undefined?extra:{...extra,'Content-Type':'application/json','X-CSRF-Token':authorization.csrf_token},
        body:body===undefined?undefined:JSON.stringify(body),signal:controller.signal});
    }catch(error){
      const writing=path.endsWith('/commands');const operationId=body?.mutation?.operation_id||null;
      throw new WorkbenchError(writing?'提交结果尚未确定，原操作编号已保留，请先核实后再继续。':'连接中断，输入已保留。',
        {code:writing?'OUTCOME_UNKNOWN':'NETWORK_UNAVAILABLE',unknown:writing,operationId});
    }finally{clearTimeout(timer)}
    let value;try{value=await response.json()}catch(error){throw new WorkbenchError('服务返回格式异常',{status:response.status,unknown:path.endsWith('/commands'),operationId:body?.mutation?.operation_id||null})}
    if(!response.ok){
      const error=value.error||value.detail||{};
      if(!options.retried&&[401,403].includes(response.status)&&['LOCAL_SESSION_REQUIRED','CSRF_REJECTED'].includes(error.code)){
        sessionPromise=null;return request(path,body,{...options,retried:true});
      }
      throw new WorkbenchError(typeof error==='string'?error:error.message||'操作未完成，请核对当前资料。',
        {code:error.code||'REQUEST_FAILED',details:error.details||{},status:response.status,unknown:error.code==='REMOTE_OUTCOME_UNKNOWN',operationId:body?.mutation?.operation_id||null});
    }
    return value;
  }
  const query=(name,args={})=>request('/api/prelive/v2/queries',{contract_version:'PRELIVE-2.3',query:name,args});
  const command=(name,args,options={})=>request('/api/prelive/v2/commands',{contract_version:'PRELIVE-2.3',command:name,args,
    mutation:{operation_id:options.operationId||uuid(),...(options.mutation||{})}},options);
  const sorted=value=>Array.isArray(value)?value.map(sorted):value&&typeof value==='object'?Object.fromEntries(Object.keys(value).sort().map(key=>[key,sorted(value[key])])):value;
  async function hash(value){const result=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify(sorted(value))));return 'sha256:'+Array.from(new Uint8Array(result),b=>b.toString(16).padStart(2,'0')).join('')}
  global.ShengboWorkbenchApi={request,query,command,hash,uuid,WorkbenchError};
})(window);
