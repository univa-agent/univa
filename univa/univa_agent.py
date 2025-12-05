import asyncio
import json
import os
import re
import traceback
from pathlib import Path
from dotenv import load_dotenv

from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field
import logging

from agno.agent import Agent
from agno.tools.mcp import MCPTools, MultiMCPTools
from agno.db.sqlite import SqliteDb

def _init_env():
    base = Path(__file__).resolve().parents[1]
    env_file = base / ".env"
    if not env_file.exists():
        raise RuntimeError("Config missing: please copy univa/.env.example to univa/.env and fill your keys.")
    load_dotenv(dotenv_path=str(env_file), override=False)

_init_env()

from univa.config.config import config
from univa.utils.model_factory import create_model

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def load_prompt(prompt_name: str) -> str:
    prompt_dir = config.get('prompt_dir')
    prompt_path = os.path.join(prompt_dir, f"{prompt_name}.txt")
    
    with open(prompt_path, 'r', encoding='utf-8') as f:
        return f.read()


def generate_todo_progress_event(plan_data: Dict) -> Dict:
    """based on plan_data generate todo_progress event"""
    if not plan_data or "execution_plan" not in plan_data:
        return None
    
    steps = plan_data["execution_plan"]["steps"]
    todo_items = []
    
    for i, step in enumerate(steps):
        status = step.get("status", "pending")
        description = step.get("action_description", f"Step {i+1}")
        
        if status == "success":
            todo_status = "completed"
        elif status == "ongoing":
            todo_status = "in_progress"
        else:
            todo_status = "pending"
        
        todo_items.append({
            "id": i,
            "description": description,
            "status": todo_status,
            "tool": step.get("tool", {}),
            "output": step.get("output", "")
        })
    
    return {
        "type": "todo_progress",
        "items": todo_items,
        "overall_description": plan_data["execution_plan"].get("overall_description", "")
    }


class PlanAgent:
    def __init__(self, mcp_tools: MultiMCPTools, plan_db):
        self.mcp_tools = mcp_tools
        
        plan_prompt = load_prompt("plan")
        
        full_instructions = f"{plan_prompt}\n"
        
        # Get model configuration from config / .env
        plan_model_provider = config.get('plan_model_provider', 'openai')
        plan_model_id = config.get('plan_model_id', 'gpt-5-2025-08-07')
        plan_model_api_key = config.get('plan_model_api_key', '')
        plan_model_base_url = config.get('plan_model_base_url', '')
        plan_model_extra_params = config.get('plan_model_extra_params', '')
        
        self.agent = Agent(
            name="Univideo Plan Agent",
            model=create_model(
                provider=plan_model_provider,
                model_id=plan_model_id,
                api_key=plan_model_api_key,
                base_url=plan_model_base_url or None,
                extra_params=plan_model_extra_params,
            ),
            instructions=full_instructions,
            db=plan_db,
            add_history_to_context=True,
            num_history_messages=10,
            session_state={
                "execution_history": []
            }
        )
    
    def _get_available_tools_description(self) -> str:
        tools_info = []
        if hasattr(self.mcp_tools, 'tools') and self.mcp_tools.tools:
            for tool in self.mcp_tools.tools:
                tools_info.append(f"- {tool.name}: {tool.description}")
        return "\n".join(tools_info) if tools_info else "No tools available"
    
    def extract_plan_from_content(self, content: str) -> Optional[Dict]:
        try:
            match = re.search(r'```json\s*(\{.*?\})\s*```', content, re.DOTALL)
            if match:
                json_str = match.group(1)
            else:
                start = content.find('{')
                end = content.rfind('}')
                if start != -1 and end != -1 and end > start:
                    json_str = content[start:end+1]
                else:
                    return content
            
            parsed_content = json.loads(json_str)
            if "execution_plan" in parsed_content:
                return parsed_content
            return content
        except (json.JSONDecodeError, Exception):
            return content
    
    async def generate_plan(self, session_id, user_request: str) -> Optional[Dict]:
        input_context = f"User Request: {user_request}\n"

        try:
            execution_historys = self.agent.get_session_state(session_id).get("execution_history", None)
        except Exception:
            execution_historys = None

        if execution_historys:
            input_context += "\n### Previous Execution Results:\n"
            input_context += json.dumps(execution_historys, indent=2, ensure_ascii=False)
            input_context += "\n\nPlease consider the above execution results when generating the new plan.\n"
            input_context += "Note that the new plan step should only include the user's latest request task and not contain steps from previous tasks."
        
        response = await self.agent.arun(
            input=input_context,
            stream=False,
            session_id=session_id
        )
        
        plan_output = response.content
        plan_output_format = self.extract_plan_from_content(plan_output)
        
        return plan_output_format
    
    def inject_execution_results(self, session_id, plan, execution_results: Dict[int, Any]) -> Dict:
            

            try:
                current_state = self.agent.get_session_state(session_id).get("execution_history", [])
            except Exception:
                current_state = []

            current_state.append(
                {
                    "plan": plan,
                    "execution_results": execution_results
                }
            )
            
            self.agent.update_session_state(
                session_state_updates={"execution_history": current_state},
                session_id=session_id
            )


class ActAgent:
    def __init__(self, mcp_tools: MultiMCPTools, act_db=None):
        self.mcp_tools = mcp_tools
        
        # Get model configuration from config / .env
        act_model_provider = config.get('act_model_provider', 'openai')
        act_model_id = config.get('act_model_id', 'gpt-5-2025-08-07')
        act_model_api_key = config.get('act_model_api_key', '')
        act_model_base_url = config.get('act_model_base_url', '')
        act_model_extra_params = config.get('act_model_extra_params', '')
        
        self.agent = Agent(
            name="Univideo Act Agent",
            model=create_model(
                provider=act_model_provider,
                model_id=act_model_id,
                api_key=act_model_api_key,
                base_url=act_model_base_url or None,
                extra_params=act_model_extra_params,
            ),
            tools=[mcp_tools],
            # instructions=act_prompt,
            instructions="""
            # Intelligent Video Task Execution Assistant

            ## Core Role Definition
            You are an intelligent video task execution assistant (Video Act LLM), specialized in executing video generation, editing, and processing plans. You follow plans provided by the Plan Model while conducting intelligent thinking, calling, wait and feedback throughout the video production workflow.

            ## Output Format
            After received the tool execution result, you should output a JSON object with the following format:
            {
                "success": "True/False",
                "message": "Like Image generated successfully.",
                "content": "The content of the tool execution result, such as the generated image URL.",
                "output_path": "The output path of the tool execution result, such as the generated image file path."
            }
            """,
            tool_call_limit=15,
        )

    
    async def execute_plan(self, question, plan) -> Dict[str, Any]:
        
        execution_results: Dict[int, Any]={}
        
        for idx, step in enumerate(plan['execution_plan']['steps']):
            try:
                logger.info(f"Executing step {idx+1}: {step.get('action_description', 'Unknown')}")
                result = await self._execute_step(step, question, plan, execution_results)

                execution_results[idx+1] = result
                plan = self.update_plan(plan, result, idx)
                logger.info(f"Step {idx+1} completed successfully")
                
            except Exception as e:
                error_msg = f"step {idx+1} execution failed: {str(e)}"
                logger.error(error_msg)
                return error_msg
        
        return execution_results
    
    async def _execute_step(self, step, question, plan, execution_results) -> Any:
        input_context = f"""
        ### User Request
        {question}

        ### Whole Plan
        {plan['execution_plan']}

        ### Completed Steps
        {execution_results}

        ### Current Step
        {step}

        You can only perform the tasks specified in the current step.
        """

        response = await self.agent.arun(
            input=input_context,
            stream=False
        )
        
        result = self.extract_json(response.content)
        
        if result is None:
            logger.warning(f"No JSON found in response, treating as text message. Content: {response.content[:200]}...")
            return {
                'success': False,
                'message': response.content,
                'content': response.content,
                'output_path': None
            }
        
        logger.info(f"Extracted result: {result}")
        
        self.current_step = {
            "step": step,
            "result": result
        }
        
        return result
    
    def extract_json(self, content: str) -> Optional[Dict]:
        try:
            match = re.search(r'```json\s*(\{.*?\})\s*```', content, re.DOTALL)
            if match:
                json_str = match.group(1)
                logger.info("Found JSON in code block format")
            else:
                start = content.find('{')
                end = content.rfind('}')
                if start != -1 and end != -1 and end > start:
                    json_str = content[start:end+1]
                    logger.info("Found JSON without code block")
                else:
                    logger.warning("No JSON structure found in content")
                    return None
            
            parsed_content = json.loads(json_str)
            logger.info(f"Successfully parsed JSON with keys: {list(parsed_content.keys())}")
            return parsed_content
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error: {e}")
            logger.error(f"Failed JSON string: {json_str[:200]}...")
            return None
        except Exception as e:
            logger.error(f"Unexpected error in extract_json: {e}")
            return None
        
    def update_plan(self, plan, step_result, idx):
        if step_result is None:
            logger.error(f"step_result is None for step {idx+1}")
            plan['execution_plan']['steps'][idx]['status'] = 'failed'
            plan['execution_plan']['steps'][idx]['output'] = 'Step result is None'
            return plan
        
        if not isinstance(step_result, dict):
            logger.error(f"step_result is not a dict for step {idx+1}: {type(step_result)}")
            plan['execution_plan']['steps'][idx]['status'] = 'failed'
            plan['execution_plan']['steps'][idx]['output'] = f'Invalid result type: {type(step_result)}'
            return plan
        
        plan['execution_plan']['steps'][idx]['status'] = step_result.get('success', False)
        plan['execution_plan']['steps'][idx]['output'] = step_result.get('output_path') or step_result.get('content', 'No output')

        return plan


class PlanActSystem:
    def __init__(self, mcp_command: List[str], db_file: str = "plan_act_system"):
        """
        init PlanActSystem
        
        Args:
            mcp_command: mcp server command list
            db_file: db file path
        """
        mcp_tools_path = config.get('mcp_tools_path')
        self.mcp_tools = MultiMCPTools(
            commands=mcp_command,
            env={
                "PYTHONPATH": mcp_tools_path,
                "CWD": mcp_tools_path
            },
            timeout_seconds=600,
            refresh_connection=True
        )
        self._plan_db = SqliteDb(db_file=f"{db_file}_plan.db")
        # self._act_db = SqliteDb(db_file=f"{db_file}_act.db")
        self.plan_agent = None
        self.act_agent = None
    
    async def __aenter__(self):
        """asynchronous context manager enter"""
        await self.mcp_tools.connect()
        
        self.plan_agent = PlanAgent(self.mcp_tools, self._plan_db)
        self.act_agent = ActAgent(self.mcp_tools)
        
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """asynchronous context manager exit"""
        await self.mcp_tools.close()
    
    async def execute_task(self, session_id, user_request: str) -> Dict[str, Any]:
        execution_plan = await self.plan_agent.generate_plan(session_id, user_request)
        
        results = await self.act_agent.execute_plan(user_request, execution_plan)

        self.plan_agent.inject_execution_results(session_id, execution_plan, results)
        
        return {
            "plan": execution_plan,
            "execution": results
        }
    
    async def execute_task_stream(self, session_id, user_request: str):
        try:
            # generate plan
            logger.info(f"[Stream] Starting task stream for session {session_id}")
            yield {'type': 'content', 'content': 'start to generate plans...'}
            
            execution_plan = await self.plan_agent.generate_plan(session_id, user_request)
            if not isinstance(execution_plan, dict) or 'execution_plan' not in execution_plan:
                logger.error(f"[Stream] Plan generation failed for session {session_id}")
                yield {'type': 'content', 'content': f'no valid plan generated.\n{execution_plan}'}
                return
            
            logger.info(f"[Stream] Plan generated successfully with {len(execution_plan.get('execution_plan', {}).get('steps', []))} steps")
            todo_events = generate_todo_progress_event(execution_plan)
            logger.info(f"[Stream] Yielding todo_progress event")
            yield todo_events
            
            # execute plan step by step
            execution_results: Dict[int, Any] = {}
            steps = execution_plan.get('execution_plan', {}).get('steps', [])
            
            for idx, step in enumerate(steps):
                step_num = idx + 1
                logger.info(f"[Stream] Starting execution of step {step_num}/{len(steps)}: {step}")
                try:
                    action_desc = step.get('action_description', f'step {step_num}')
                    tool_name = step.get('tool', {}).get('name', 'unknown')
                    
                    logger.info(f"[Stream] Yielding tool_start event for step {step_num}: {tool_name}")
                    tool_start_event = {'type': 'tool_start', 'tool': tool_name}
                    logger.info(f"[Stream] tool_start event data: {tool_start_event}")
                    yield tool_start_event
                    
                    logger.info(f"[Stream] Executing step {step_num}: {action_desc}")
                    result = await self.act_agent._execute_step(
                        step, user_request, execution_plan, execution_results
                    )
                    
                    execution_results[step_num] = result
                    execution_plan = self.act_agent.update_plan(execution_plan, result, idx)
                    
                    logger.info(f"[Stream] Yielding tool_end event for step {step_num}")
                    tool_end_event = {'type': 'tool_end', 'result': result}
                    logger.info(f"[Stream] tool_end event data: {tool_end_event}")
                    yield tool_end_event
                    
                    # check step result, if failed then return value
                    if result and isinstance(result, dict):
                        success = result.get('success')
                        is_success = success in [True, 'True', 'true']
                        
                        if not is_success:
                            error_msg = result.get('message', 'Step execution failed')
                            logger.error(f"[Stream] Step {step_num} failed: {error_msg}")
                            yield {
                                'type': 'error',
                                'content': f"Task failed at step {step_num}: {error_msg}"
                            }
                            return
                    
                    logger.info(f"[Stream] Step {step_num} completed successfully")
                    
                except Exception as e:
                    error_msg = f"step {idx+1} execution failed: {str(e)}"
                    logger.error(f"[Stream] {error_msg}")
                    logger.error(traceback.format_exc())
                    yield {'type': 'error', 'content': f"Error executing step {idx+1}\n {error_msg}"}
                    return
            
            # inject results back to plan agent
            logger.info(f"[Stream] Injecting execution results back to plan agent")
            self.plan_agent.inject_execution_results(session_id, execution_plan, execution_results)

            logger.info(f"[Stream] Yielding finish event for session {session_id}")
            finish_event = {'type': 'finish', 'session_id': session_id}
            logger.info(f"[Stream] finish event data: {finish_event}")
            yield finish_event
            
            logger.info(f"[Stream] Task stream completed successfully for session {session_id}")
            
        except Exception as e:
            logger.error(f"[Stream] Error in execute_task_stream: {e}")
            logger.error(traceback.format_exc())
            yield {
                'type': 'error',
                'content': str(e)
            }


async def initialize_global_agents() -> PlanActSystem:
    # load MCP server configurations
    config_path = config.get('mcp_servers_config')
    
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            mcp_config = json.load(f)
        
        mcp_servers = mcp_config.get("mcpServers", {})
        logger.info(f"Loaded {len(mcp_servers)} MCP servers from config")
        
        # construct mcp commands
        mcp_commands = []
        for server_name, server_config in mcp_servers.items():
            command = server_config.get("command", "")
            args = server_config.get("args", [])
            env = server_config.get("env", {})
            
            # full command with args
            full_command = f"{command} {' '.join(args)}"
            
            mcp_commands.append(full_command)
            logger.info(f"Registered MCP server '{server_name}': {full_command}")
        
        mcp_command = mcp_commands[0] if mcp_commands else "npx -y @modelcontextprotocol/server-filesystem /tmp"
        
    except FileNotFoundError:
        logger.warning(f"MCP config file not found: {config_path}, using default")
        mcp_command = "npx -y @modelcontextprotocol/server-filesystem /tmp"
    except Exception as e:
        logger.error(f"Error loading MCP config: {e}, using default")
        mcp_command = "npx -y @modelcontextprotocol/server-filesystem /tmp"
    
    global_plan_act_system = PlanActSystem(mcp_command=mcp_commands)
    await global_plan_act_system.__aenter__()
    
    logger.info("Global PlanActSystem initialized")
    
    return global_plan_act_system


async def main():
    system = await initialize_global_agents()

    session_id = "test_interactive_session_001"
    
    try:
        print("system is ready. You can start inputting tasks (type 'exit' or 'quit' to stop).")
        while True:
            try:
                input_prompt = input("\nUser (please input propmt): ")
                if input_prompt.lower() in ['exit', 'quit']:
                    break
                
                if not input_prompt.strip():
                    continue

                print(f"processing: {input_prompt} ...")
                
                result = await system.execute_task(session_id, input_prompt)
                
                print("\n" + "="*60)
                print("📊 Result:")
                print(result)
                print("completed.")
                print("="*60)

            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"error: {e}")
    finally:
        await system.__aexit__(None, None, None)


if __name__ == "__main__":
    asyncio.run(main())

