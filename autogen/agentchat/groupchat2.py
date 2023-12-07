import logging
import random
import re
import sys
import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Union

from ..code_utils import content_str
from .agent import Agent
from .conversable_agent import ConversableAgent

logger = logging.getLogger(__name__)


@dataclass
class GroupChat2:
    """(In preview) A group chat class that contains the following data fields:
    - agents: a list of participating agents.
    - messages: a list of messages in the group chat.
    - max_round: the maximum number of rounds.
    - admin_name: the name of the admin agent if there is one. Default is "Admin".
        KeyBoardInterrupt will make the admin agent take over.
    - func_call_filter: whether to enforce function call filter. Default is True.
        When set to True and when a message is a function call suggestion,
        the next speaker will be chosen from an agent which contains the corresponding function name
        in its `function_map`.
    - speaker_selection_method: the method for selecting the next speaker. Default is "auto".
        Could be any of the following (case insensitive), will raise ValueError if not recognized:
        - "auto": the next speaker is selected automatically by LLM.
        - "manual": the next speaker is selected manually by user input.
        - "random": the next speaker is selected randomly.
        - "round_robin": the next speaker is selected in a round robin fashion, i.e., iterating in the same order as provided in `agents`.
    - allow_repeat_speaker: whether to allow the same speaker to speak consecutively. Default is True.
    """

    agents: List[Agent]
    messages: List[Dict]
    max_round: int = 10
    admin_name: str = "Admin"
    func_call_filter: bool = True
    speaker_selection_method: str = "auto"
    allow_repeat_speaker: Optional[Union[bool, List[Agent]]] = True

    _VALID_SPEAKER_SELECTION_METHODS = ["auto", "manual", "random", "round_robin"]

    @property
    def agent_names(self) -> List[str]:
        """Return the names of the agents in the group chat."""
        return [agent.name for agent in self.agents]

    def reset(self):
        """Reset the group chat."""
        self.messages.clear()

    def append(self, message: Dict):
        """Append a message to the group chat.
        We cast the content to str here so that it can be managed by text-based
        model.
        """
        message["content"] = content_str(message["content"])
        self.messages.append(message)

    def agent_by_name(self, name: str) -> Agent:
        """Returns the agent with a given name."""
        return self.agents[self.agent_names.index(name)]

    def next_agent(self, agent: Agent, agents: List[Agent]) -> Agent:
        """Return the next agent in the list."""
        if agents == self.agents:
            offset = 0
            try:
                offset = self.agent_names.index(agent.name)
            except ValueError:
                pass

            return agents[(offset + 1) % len(agents)]
        else:
            offset = 0
            try:
                offset = self.agent_names.index(agent.name) + 1
            except ValueError:
                pass

            for i in range(len(self.agents)):
                if self.agents[(offset + i) % len(self.agents)] in agents:
                    return self.agents[(offset + i) % len(self.agents)]

    def intro_msg(self):
        """Return the introduction message that every agent receives at the start of the chat."""
        return f"""Hello everyone. We have assembled a great team today to answer questions and solve tasks. In attendance are:

{self._participant_roles(self.agents)}
"""

    def select_speaker_msg(self, agents: List[Agent]):
        """Return the system message for selecting the next speaker. This is always the *first* message in the context."""
        return f"""You are moderating a conversation between {len(self.agents)} participants who are working together to answer questions and perform tasks. Your role is as a moderator. DON'T DIRECTLY ANSWER THE QUESTIONS OR PERFORM ANY OF THE WORK YOURSELF. IN PARTICULAR, DO NOT WRITE ANY CODE YOURSELF. INSTEAD, DIRECT THE PARTICIPANTS TO DO SO, AS APPROPRIATE. In attendance are the following participants:

{self._participant_roles(agents)}

Read the following conversation, then carefully consider who you should speak to next, and what you should ask of them, so as to make the most progress on the task). Speakers do not need equal speaking time. You may even ignore non-relevant participants. Your focus is on efficiently driving progress toward task completion.

After each participant response, decide the following:
    - WHO should speak next? (A valid participant name, selected from this list: {[agent.name for agent in agents]})
    - WHAT should you ask of them? (phrased the way you would actually ask them in conversation)
    - WHY it makes sense to ask them at this moment (your internal reasoning)

Your output should be a perfect JSON object as per below:
    {{
        "why": your_reasoning,
        "who": participant_name,
        "what": your_question_or_request
    }}
DO NOT OUTPUT ANYTHING OTHER THAN THIS JSON OBJECT. YOUR OUTPUT MUST BE PARSABLE AS JSON.
"""

    def select_speaker_prompt(self, agents: List[Agent], excluded_agent: Optional[Union[Agent, None]] = None):
        """Return the floating system prompt selecting the next speaker. This is always the *last* message in the context."""

        exclude_speaker_msg = ""
        if excluded_agent is not None:
            exclude_speaker_msg = f"\nNote: Don't ask {excluded_agent.name} again, since they just spoke. Instead ask {' or '.join([agent.name for agent in agents])}."

        return f"""Remember, YOUR role is to serve as a moderator. DON'T ANSWER QUESTIONS, CODE, OR PERFORM OTHER WORK YOURSELF. Instead, read the above conversation, then carefully decide the following, with a focus on making progress on the task:

    - WHO should speak next? (A valid participant name, selected from this list: {[agent.name for agent in agents]})
    - WHAT should you ask of them? (phrased the way you would actually ask them in conversation)
    - WHY it makes sense to ask them at this moment (your internal reasoning)
{exclude_speaker_msg}

Your output should be a perfect JSON object as per below:
    {{
        "why": your_reasoning,
        "who": participant_name,
        "what": your_question_or_request
    }}


DO NOT OUTPUT ANYTHING OTHER THAN THIS JSON OBJECT. YOUR OUTPUT MUST BE PARSABLE AS JSON.
"""

    def manual_select_speaker(self, agents: List[Agent]) -> Union[Agent, None]:
        """Manually select the next speaker."""

        print("Please select the next speaker from the following list:")
        _n_agents = len(agents)
        for i in range(_n_agents):
            print(f"{i+1}: {agents[i].name}")
        try_count = 0
        # Assume the user will enter a valid number within 3 tries, otherwise use auto selection to avoid blocking.
        while try_count <= 3:
            try_count += 1
            if try_count >= 3:
                print(f"You have tried {try_count} times. The next speaker will be selected automatically.")
                break
            try:
                i = input("Enter the number of the next speaker (enter nothing or `q` to use auto selection): ")
                if i == "" or i == "q":
                    break
                i = int(i)
                if i > 0 and i <= _n_agents:
                    return agents[i - 1]
                else:
                    raise ValueError
            except ValueError:
                print(f"Invalid input. Please enter a number between 1 and {_n_agents}.")
        return None

    def select_speaker(self, last_speaker: Agent, selector: ConversableAgent):
        """Select the next speaker."""
        if self.speaker_selection_method.lower() not in self._VALID_SPEAKER_SELECTION_METHODS:
            raise ValueError(
                f"GroupChat2 speaker_selection_method is set to '{self.speaker_selection_method}'. "
                f"It should be one of {self._VALID_SPEAKER_SELECTION_METHODS} (case insensitive). "
            )

        # If provided a list, make sure the agent is in the list
        allow_repeat_speaker = (
            self.allow_repeat_speaker
            if isinstance(self.allow_repeat_speaker, bool)
            else last_speaker in self.allow_repeat_speaker
        )

        agents = self.agents
        n_agents = len(agents)
        # Warn if GroupChat2 is underpopulated
        if n_agents < 2:
            raise ValueError(
                f"GroupChat2 is underpopulated with {n_agents} agents. "
                "Please add more agents to the GroupChat2 or use direct communication instead."
            )
        elif n_agents == 2 and self.speaker_selection_method.lower() != "round_robin" and allow_repeat_speaker:
            logger.warning(
                f"GroupChat2 is underpopulated with {n_agents} agents. "
                "It is recommended to set speaker_selection_method to 'round_robin' or allow_repeat_speaker to False."
                "Or, use direct communication instead."
            )

        if self.func_call_filter and self.messages and "function_call" in self.messages[-1]:
            # find agents with the right function_map which contains the function name
            agents = [
                agent for agent in self.agents if agent.can_execute_function(self.messages[-1]["function_call"]["name"])
            ]
            if len(agents) == 1:
                # only one agent can execute the function
                return (agents[0], None)
            elif not agents:
                # find all the agents with function_map
                agents = [agent for agent in self.agents if agent.function_map]
                if len(agents) == 1:
                    return (agents[0], None)
                elif not agents:
                    raise ValueError(
                        f"No agent can execute the function {self.messages[-1]['name']}. "
                        "Please check the function_map of the agents."
                    )

        # remove the last speaker from the list to avoid selecting the same speaker if allow_repeat_speaker is False
        agents = agents if allow_repeat_speaker else [agent for agent in agents if agent != last_speaker]

        if self.speaker_selection_method.lower() == "manual":
            selected_agent = self.manual_select_speaker(agents)
            if selected_agent:
                return (selected_agent, None)
        elif self.speaker_selection_method.lower() == "round_robin":
            return (self.next_agent(last_speaker, agents), None)
        elif self.speaker_selection_method.lower() == "random":
            return (random.choice(agents), None)

        # auto speaker selection
        selector.update_system_message(self.select_speaker_msg(agents))
        context = self.messages + [
            {
                "role": "system",
                "content": self.select_speaker_prompt(agents, None if allow_repeat_speaker else last_speaker),
            }
        ]
        # print(json.dumps(selector._oai_system_message + context, indent=4))
        final, response = selector.generate_oai_reply(context)

        if not final:
            # the LLM client is None, thus no reply is generated. Use round robin instead.
            return (self.next_agent(last_speaker, agents), None)

        # Parse the response
        try:
            # print(json.dumps(response, indent=4))
            parsed_response = json.loads(response)
        except json.decoder.JSONDecodeError:
            logger.warning(f"Failed to parse:\n{response}")
            return (self.next_agent(last_speaker, agents), None)

        # Return the result
        try:
            return (self.agent_by_name(parsed_response["who"]), parsed_response["what"])
        except ValueError:
            return (self.next_agent(last_speaker, agents), None)

    def _participant_roles(self, agents: List[Agent] = None) -> str:
        # Default to all agents registered
        if agents is None:
            agents = self.agents

        roles = []
        for agent in agents:
            if agent.description.strip() == "":
                logger.warning(
                    f"The agent '{agent.name}' has an empty description, and may not work well with GroupChat2."
                )
            roles.append(f"{agent.name}: {agent.description}".strip())
        return "\n".join(roles)

    def _mentioned_agents(self, message_content: Union[str, List], agents: List[Agent]) -> Dict:
        """Counts the number of times each agent is mentioned in the provided message content.

        Args:
            message_content (Union[str, List]): The content of the message, either as a single string or a list of strings.
            agents (List[Agent]): A list of Agent objects, each having a 'name' attribute to be searched in the message content.

        Returns:
            Dict: a counter for mentioned agents.
        """
        # Cast message content to str
        message_content = content_str(message_content)

        mentions = dict()
        for agent in agents:
            regex = (
                r"(?<=\W)" + re.escape(agent.name) + r"(?=\W)"
            )  # Finds agent mentions, taking word boundaries into account
            count = len(re.findall(regex, f" {message_content} "))  # Pad the message to help with matching
            if count > 0:
                mentions[agent.name] = count
        return mentions


class GroupChatManager2(ConversableAgent):
    """(In preview) A chat manager agent that can manage a group chat of multiple agents."""

    def __init__(
        self,
        groupchat: GroupChat2,
        name: Optional[str] = "chat_manager",
        # unlimited consecutive auto reply by default
        max_consecutive_auto_reply: Optional[int] = sys.maxsize,
        human_input_mode: Optional[str] = "NEVER",
        system_message: Optional[Union[str, List]] = "Group chat manager.",
        **kwargs,
    ):
        super().__init__(
            name=name,
            max_consecutive_auto_reply=max_consecutive_auto_reply,
            human_input_mode=human_input_mode,
            system_message=system_message,
            **kwargs,
        )
        # Order of register_reply is important.
        # Allow sync chat if initiated using initiate_chat
        self.register_reply(Agent, GroupChatManager2.run_chat, config=groupchat, reset_config=GroupChat2.reset)
        # Allow async chat if initiated using a_initiate_chat
        self.register_reply(Agent, GroupChatManager2.a_run_chat, config=groupchat, reset_config=GroupChat2.reset)

    def run_chat(
        self,
        messages: Optional[List[Dict]] = None,
        sender: Optional[Agent] = None,
        config: Optional[GroupChat2] = None,
    ) -> Union[str, Dict, None]:
        """Run a group chat."""
        if messages is None:
            messages = self._oai_messages[sender]
        message = messages[-1]
        speaker = sender
        groupchat = config

        # Broadcast the intro
        intro = {"role": "user", "name": self.name, "content": groupchat.intro_msg()}
        first = True
        for agent in groupchat.agents:
            self.send(intro, agent, request_reply=False, silent=(not first))
            first = False

        for i in range(groupchat.max_round):
            # set the name to speaker's name if the role is not function
            if message["role"] != "function":
                message["name"] = speaker.name

            groupchat.append(message)
            if self._is_termination_msg(message):
                # The conversation is over
                break

            # broadcast the message to all agents except the speaker
            for agent in groupchat.agents:
                if agent != speaker:
                    self.send(message, agent, request_reply=False, silent=True)
            if i == groupchat.max_round - 1:
                # the last round
                break

            # select the next speaker
            (speaker, moderator_message) = groupchat.select_speaker(speaker, self)

            # If the message isn't none, broadcast it too
            if moderator_message is not None:
                moderator_message = {"role": "user", "name": self.name, "content": moderator_message}
                for agent in groupchat.agents:
                    if agent == speaker:
                        self.send(moderator_message, agent, request_reply=False, silent=False)
                    else:
                        self.send(moderator_message, agent, request_reply=False, silent=True)

                # Switch roles
                moderator_message = moderator_message.copy()
                moderator_message["role"] = "assistant"
                groupchat.append(moderator_message)
            try:
                # let the speaker speak
                reply = speaker.generate_reply(sender=self)
            except KeyboardInterrupt:
                # let the admin agent speak if interrupted
                if groupchat.admin_name in groupchat.agent_names:
                    # admin agent is one of the participants
                    speaker = groupchat.agent_by_name(groupchat.admin_name)
                    reply = speaker.generate_reply(sender=self)
                else:
                    # admin agent is not found in the participants
                    raise
            if reply is None:
                break
            # The speaker sends the message without requesting a reply
            speaker.send(reply, self, request_reply=False)
            message = self.last_message(speaker)
        return True, None

    async def a_run_chat(
        self,
        messages: Optional[List[Dict]] = None,
        sender: Optional[Agent] = None,
        config: Optional[GroupChat2] = None,
    ):
        """Run a group chat asynchronously."""
        if messages is None:
            messages = self._oai_messages[sender]
        message = messages[-1]
        speaker = sender
        groupchat = config
        for i in range(groupchat.max_round):
            # set the name to speaker's name if the role is not function
            if message["role"] != "function":
                message["name"] = speaker.name

            groupchat.append(message)

            if self._is_termination_msg(message):
                # The conversation is over
                break

            # broadcast the message to all agents except the speaker
            for agent in groupchat.agents:
                if agent != speaker:
                    await self.a_send(message, agent, request_reply=False, silent=True)
            if i == groupchat.max_round - 1:
                # the last round
                break
            try:
                # select the next speaker
                speaker = groupchat.select_speaker(speaker, self)
                # let the speaker speak
                reply = await speaker.a_generate_reply(sender=self)
            except KeyboardInterrupt:
                # let the admin agent speak if interrupted
                if groupchat.admin_name in groupchat.agent_names:
                    # admin agent is one of the participants
                    speaker = groupchat.agent_by_name(groupchat.admin_name)
                    reply = await speaker.a_generate_reply(sender=self)
                else:
                    # admin agent is not found in the participants
                    raise
            if reply is None:
                break
            # The speaker sends the message without requesting a reply
            await speaker.a_send(reply, self, request_reply=False)
            message = self.last_message(speaker)
        return True, None
