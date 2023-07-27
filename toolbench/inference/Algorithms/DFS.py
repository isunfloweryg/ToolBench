import re
from Tree.Tree import my_tree, tree_node
from Prompts.ReAct_prompts import FORMAT_INSTRUCTIONS_SYSTEM_FUNCTION, FORMAT_INSTRUCTIONS_USER_FUNCTION
from Prompts.Tree_search_prompts import  DIVERSITY_PROMPT
from Algorithms.base_search import base_search_method
from copy import deepcopy
from LLM_rank.rank_candidate import sum_based_rankn, rank2_subfix
import json
import random

class DFS_tree_search(base_search_method):

    def __init__(self,llm,io_func,process_id=0):
        super(DFS_tree_search, self).__init__()
        '''
        Depth-first search. Every time a child node is generated, choose the best multiple iterations to go.
        '''
        self.io_func = io_func
        self.llm = llm
        self.process_id=process_id
        self.restart()
    def restart(self):
        self.status = 0
        self.terminal_node = []
        self.give_up_node = []
        self.now_expand_num = 0
        self.query_count = 0
        self.total_tokens = 0

    def to_json(self,answer=False,process=True):

        if process:
            json_obj = {
                "win": self.status == 1,
                "tree": self.tree.to_json_recursive(),
                "forward_args":self.forward_args,
                "compare_candidates": [],
            }
            for node in self.terminal_node:
                if node.pruned == False: # has answer
                    json_obj["compare_candidates"].append(node.get_chain_result_from_this_node(use_messages=False))
        else:
            json_obj = {}

            
        if answer:
            json_obj["answer_generation"] = {
                "valid_data": False,
                "query_count": self.query_count,
                "total_tokens": self.total_tokens,
                "final_answer": "",
                "finish_type":"give_answer",
                "function": self.io_func.functions,
                "chain": [],
            }
            for node in self.terminal_node:
                if node.pruned == False:
                    json_obj["answer_generation"]["valid_data"] = True
                    json_obj["answer_generation"]["finish_type"] = "give_answer"
                    json_obj["answer_generation"]["final_answer"] = node.description
                    json_obj["answer_generation"]["train_messages"] = node.get_train_messages_from_this_node()
                    break
            if json_obj["answer_generation"]["valid_data"] == False: # do not have final answer, look for give_up
                if len(self.give_up_node) > 0:
                    random_pos = random.randint(0,len(self.give_up_node) - 1)
                    choose_give_up_node = self.give_up_node[random_pos]
                    json_obj["answer_generation"]["valid_data"] = True
                    json_obj["answer_generation"]["finish_type"] = "give_up"
                    json_obj["answer_generation"]["final_answer"] = choose_give_up_node.description
                    json_obj["answer_generation"]["train_messages"] = choose_give_up_node.get_train_messages_from_this_node()
        return json_obj

    def start(self,single_chain_max_step, tree_beam_size,max_query_count, answer=1,with_filter=True):
        '''
        single_chain_max_step: The maximum depth of the tree
        tree_beam_size: How many children nodes are generated per layer
        '''
        self.forward_args = locals()
        if "self" in self.forward_args.keys():
            self.forward_args.pop("self")
        self.tree = my_tree()
        self.tree.root.node_type = "Action Input"
        self.tree.root.io_state = deepcopy(self.io_func)
        
        '''
        initialize root's self.messages to generate system and user
        '''
        system = FORMAT_INSTRUCTIONS_SYSTEM_FUNCTION
        system = system.replace("{task_description}",self.io_func.task_description)
        self.tree.root.messages.append({"role":"system","content":system})

        user = FORMAT_INSTRUCTIONS_USER_FUNCTION
        user = user.replace("{input_description}",self.io_func.input_description)
        self.tree.root.messages.append({"role":"user","content":user})


        return self.DFS(self.tree.root,single_chain_max_step, tree_beam_size,max_query_count,answer,with_filter )


    def DFS(self, now_node,single_chain_max_step, tree_beam_size,max_query_count,answer,with_filter=True):
        '''
        Returns the number of grids to go back. When a child node of a node generates a final answer or give up, it should go back a few more grids
        In a sense, the larger this value is, the more diverse it is, and it is GreedySearch@n when it is enlarged to infinity.
        '''

        final_answer_back_length = 2
        prune_back_length = 2

        now_node.expand_num = self.now_expand_num
        self.now_expand_num += 1
        if now_node.get_depth() >= single_chain_max_step or now_node.pruned or now_node.is_terminal:
            if now_node.is_terminal: # final answer
                self.status = 1
                self.terminal_node.append(now_node)
                return final_answer_back_length
            else:
                now_node.pruned = True
                if now_node.observation_code == 4:
                    self.give_up_node.append(now_node)
                    return prune_back_length
                else:
                    return 1
        
        '''
        Generate children
        '''
        next_tree_split_nodes = []
        for i in range(tree_beam_size):
            temp_now_node = now_node
            '''
            increase the diversity of prompt
            '''
            delete_former_diversity_message = False
            diversity_message = None
            if len(temp_now_node.children) > 0:
                
                former_candidates_des = ""
                js_list = []
                for k, child in enumerate(temp_now_node.children):
                    temp_node = child
                    while not temp_node.is_terminal and temp_node.node_type != "Action Input" and len(temp_node.children) > 0:
                        temp_node = temp_node.children[0]
                    if temp_node.node_type == "Action Input":
                        obj_dict = {
                            "name": temp_node.father.description,
                            "arguments": temp_node.description,
                            "function_output": temp_node.observation,
                            "mento-carlo-action-value": temp_node.compute_weight(),
                        }
                        js_list.append(obj_dict)
                
                if len(js_list) > 0:
                    former_candidates_des = former_candidates_des + f"{json.dumps(js_list,indent=2)}\n"
                    if temp_now_node.observation != "":
                        former_candidates_des = former_candidates_des + f"again, your former observation: {temp_now_node.observation}\n"
                    diverse_prompt = DIVERSITY_PROMPT
                    diverse_prompt = diverse_prompt.replace("{previous_candidate}",former_candidates_des)
                    diversity_message = {"role":"user", "content":diverse_prompt}
                    temp_now_node.messages.append(diversity_message)

                    delete_former_diversity_message = True
                    
            self.llm.change_messages(temp_now_node.messages)
            new_message,error_code,total_tokens = self.llm.parse(self.io_func.functions, process_id=self.process_id)
            self.query_count += 1
            self.total_tokens += total_tokens
            if self.query_count >= max_query_count:
                return 100000

            if delete_former_diversity_message:
                temp_now_node.messages[-1]["valid"] = False

            assert new_message["role"] == "assistant"
            if "content" in new_message.keys() and new_message["content"] != None:
                temp_node = tree_node()
                temp_node.node_type = "Thought"
                temp_node.description = new_message["content"]
                child_io_state = deepcopy(temp_now_node.io_state)
                
                temp_node.io_state = child_io_state
                temp_node.is_terminal = child_io_state.check_success() != 0 
                temp_node.messages = deepcopy(temp_now_node.messages)
                temp_node.father = temp_now_node
                temp_now_node.children.append(temp_node)
                temp_node.print(self.process_id)
                temp_now_node = temp_node

                if error_code != 0:
                    temp_now_node.observation_code = error_code
                    temp_now_node.pruned = True

            if "function_call" in new_message.keys():
                function_name = new_message["function_call"]["name"]
                temp_node = tree_node()
                temp_node.node_type = "Action"
                temp_node.description = function_name
                child_io_state = deepcopy(temp_now_node.io_state)
                
                temp_node.io_state = child_io_state
                temp_node.is_terminal = child_io_state.check_success() != 0 
                temp_node.messages = deepcopy(temp_now_node.messages)
                temp_node.father = temp_now_node
                temp_now_node.children.append(temp_node)

                temp_node.print(self.process_id)
                temp_now_node = temp_node

                function_input = new_message["function_call"]["arguments"]
                temp_node = tree_node()
                temp_node.node_type = "Action Input"
                temp_node.description = function_input
                child_io_state = deepcopy(temp_now_node.io_state)

                observation, status = child_io_state.step(action_name=temp_now_node.description, action_input=function_input)
                temp_node.observation = observation
                temp_node.observation_code = status

                temp_node.io_state = child_io_state
                temp_node.is_terminal = child_io_state.check_success() != 0 
                temp_node.messages = deepcopy(temp_now_node.messages)
                temp_node.father = temp_now_node
                temp_now_node.children.append(temp_node)
                temp_node.print(self.process_id)
                temp_now_node = temp_node

                if status != 0:
                    # 0 means normal return
                    # 1 means there is no corresponding api name
                    # 2 means there is an error in the input
                    # 3 represents the end of the generation, and the final answer appears
                    # 4 means that the model decides to pruning by itself
                    if status == 4:
                        temp_now_node.pruned = True
                    elif status == 1: # hallucination api name
                        assert "function_call" in new_message.keys()
                        new_message["function_call"]["name"] = "invalid_hallucination_function_name"
                    elif status == 3: # final answer
                        temp_now_node.is_terminal = True
                        temp_now_node.make_finish(final_answer_back_length)
            
            temp_now_node.messages.append(new_message)
            if temp_now_node.node_type == "Action Input":
                temp_now_node.messages.append({
                    "role":"function",
                    "name": new_message["function_call"]["name"],
                    "content": temp_now_node.observation,
                })

            if not with_filter:
                result = self.DFS(temp_now_node,single_chain_max_step, tree_beam_size,max_query_count,answer,with_filter)
                if len(self.terminal_node) >= answer:
                    return 10000
                elif result > 1:
                    return result - 1

            else:

                next_tree_split_nodes.append(temp_now_node)
        
        '''
        Sort the generated next_tree_split_nodes nodes
        '''
        if len(next_tree_split_nodes) > 1:
            LLM_rank_args = {
                "functions": self.io_func.functions,
                "process_id": self.process_id,
                "task_description": self.io_func.task_description,
                "rank_func": rank2_subfix,
            }
            scores, rank_query_count,total_tokens = sum_based_rankn(self.llm,LLM_rank_args=LLM_rank_args,candidates=next_tree_split_nodes)
            self.query_count += rank_query_count
            self.total_tokens += total_tokens
            for score, node in zip(scores, next_tree_split_nodes):
                node.prior_score = score
            zip_value = list(zip(next_tree_split_nodes,range(len(next_tree_split_nodes))))
            zip_value.sort(key=lambda x: x[0].prior_score, reverse=True) #先做score高的
            next_tree_split_nodes,filtered_order = zip(*zip_value)
            if self.process_id == 0:
                print(f"score={scores}, filtered order: {filtered_order}")

        '''
        Choose one to expand
        '''
        for i in range(len(next_tree_split_nodes)):
            result = self.DFS(next_tree_split_nodes[i],single_chain_max_step, tree_beam_size,max_query_count,answer)
            if len(self.terminal_node) >= answer:
                return 10000
            elif result > 1:
                now_node.make_finish(2)
                return result - 1
            
        return 1


    