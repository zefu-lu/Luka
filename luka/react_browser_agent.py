from pydantic import BaseModel, Field
from typing import Tuple, List, Optional

import instructor
import os
from litellm import completion, encode
from termcolor import colored
from datetime import datetime

from luka.tools import SeleniumSandbox
from luka.memory import FIFOConversationMemory, TextEditorMemory
from luka.utils import Message


SYSTEM_PROMPT = """
You are an agent controlling a browser. You are given:

	(1) an objective that you are trying to achieve
	(2) a simplified DOM of what's visible in the browser window (more on that below)
    (3) a history of previous interactions that lead you to the current state
    (4) a blank txt file that you can edit and read from, useful for storing information

The format of the browser content is highly simplified; all formatting elements are stripped.
Interactive elements such as links, inputs are represented like this:

    <link id=1>text</link>
    <input id=2>text</input>

The list of previous interactions is an interleave of your explanations, your commands, and 
the browser's responses. It might also contain sporadic messages from the user. The browser's 
responses never contain the actual DOM, but only give you a high-level description or any 
errors occurred. e.g.:

    [2024-05-01 15:30:00] user:   Buy me a box of paperclips on Amazon
    [2024-05-01 15:35:00] agent:  First, I need to get to the Amazon website.
    [2024-05-01 15:35:30] agent:  VISIT www.amazon.com
    [2024-05-01 15:35:30] chrome: Success. 
                                  Current page: www.amazon.com
                                  Current scroll position: 0% (scroll-y=0, scroll-height=2094)

The txt file is a text file that you can edit and read from. Each line is numbered, and you can
refer to the line number to perform actions on the file such as insert and replace. The file 
will be provided to you in the subsequent steps. Use it to store important information that helps
you achieve the objective, e.g., if the user asks you to collect some information that requires
multiple steps to gather, the file can be useful to store intermediate results.                                  

You can issue these commands:

    Browser commands:
    VISIT <URL> - visit a new URL
	SUP - scroll up one page
	SDOWN - scroll down one page
	CLICK <ID> - click on a given element. You can only click on links!
	TYPE <ID> <TEXT> - type the specified text into the input with id
	TYPESUBMIT <ID> <TEXT> - same as TYPE above, except then it presses ENTER to submit the form
    BACK - go back to the previous page
    FORWARD - go forward to the next page

    Txt file edit commands:
    TINSERT <LINE_NO> <TEXT> - insert text at the specified line number
    TREPLACE <FROM_LINE_NO> <TO_LINE_NO> <TEXT> - replace existing text from the range with new text

    User interaction commands:
    YIELD <TEXT> - yield control to user with a message in <TEXT>;
    ASK <TEXT> - ask the user a question in <TEXT>; 
    COMPLETE <TEXT> - indicate that you have completed the objective and provide any comments in <TEXT>

IMPORTANT: Based on your given objective, you must first provide a rationale in text for the next
action, then issue any command that you beleive will get you closer to achieving the goal. The rationale 
and command will be added to the history of interactions for your reference at future steps. The 
rationale should be kept concise, less than 30 words, and must be a natural continuation of previous 
interactions.

Note: 
* You start on about:blank, but you can visit any site directly. Usually you should start on 
  google.com and search from there. Don't try to interact with elements that you can't see. 
* You must make appropriate adjustment if the user provides additional information, changes the objective,
  or specifies a concrete way of achieving the goal.
* If you believe you have reached the objective, issue a `COMPLETE` command with any additional comments.
  Note that the txt file will be provided to the user at the end of the session. So you can store anything
  that you think is important for the user to know in the txt file.
* If you encounter a CAPTCHA, sign-in with username/email/password, or any other user-specific interaction, 
  issue a `YIELD` command. Avoid creating accounts or entering personal information unless told to do so.
  Only issue `YIELD` command when you encounter `I cannot possibly proceed without your help` situation.
* Avoid entering made-up information, especially when asked for personal information.
* If you need clarification or want to present the user with choices, issue an `ASK` command. Basically, 
  only invoke `ASK` command when you encounter `How should I proceed?` situation, e.g., signing up for
  a website, choosing a product to buy, clairfying the objective, etc.
* If you encounter an exception, an effectless command, or find yourself in a loop, avoid repeating the 
  same command and try something else to achieve the goal.
* When you are exploring a website in order to gather information, you must issue `TINSERT` or `TREPLACE`
  commands to edit the txt file in order to store the information you have gathered.
* Avoid unnecessary `TREPLACE` commands. Only use it when you are editing existing information.

The current browser content, history of interactions, and objective follow. 
Reply with your rationale and issue the next command to the browser.
"""

USER_PROMPT = """ 
------------------
CURRENT BROWSER CONTENT:
$dom
------------------
HISTORY:
$history
------------------
TXT FILE:
$txt_file
------------------
OBJECTIVE:
$objective
------------------
YOUR COMMAND:
"""

class _AgentReply(BaseModel):
    rationale: str = Field(..., description="The rationale behind the command")
    command: str = Field(..., description="The command to execute, e.g., VISIT, CLICK, COMPLETE, etc.")
    args: List[str] = Field([], description="Arguments for the command, e.g., URL, ID, TEXT, etc. If no arguments are needed, this field is empty list. TEXT arguments should not be split into separate words.")


class ReActBrowserAgent:

    def __init__(self):
        self._sandbox = SeleniumSandbox()

        self._model = "gpt-4o"
        self._openai_key = os.getenv("OPENAI_API_KEY")
        self._client = instructor.from_litellm(completion)
        
        def litellm_tokenize(x):
            return encode(model=self._model, text=x)
        
        def litellm_summarize(msg_list):
            prompt = """
            You are given a history of previous interactions among an agent, a user, and a browser. The 
            agent is controlling a browser to achieve a given objective specified by the user. The agent
            can issue commands to the browser, and the browser can respond with high-level descriptions,
            errors, or other messages. The user specifies the objective at first and can provide additional
            information to help the agent along the way. 
            You goal is to summarize the given messages in three sentences. The reader of the message should
            be able to understand the objective, the actions the agent has attempted so far, and the current
            progress towards completing the objective. 
            Now, summarize the given messages in three sentences.
            """
            msg_str = "\n".join([str(msg) for msg in msg_list])
            response = completion(
                model = self._model,
                messages = [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": msg_str}
                ]
            )
            summary = response["choices"][0]["message"]["content"]
            return summary
        
        self._fifo_mem = FIFOConversationMemory(
            tokenize=litellm_tokenize, 
            summarize=litellm_summarize, 
            max_size=1024, 
            trigger_threshold=0.8, 
            target_threshold=0.5
        )
        self._txt_mem = TextEditorMemory(tokenize=litellm_tokenize, max_size=1024)
    
    def reset(self):
        self._sandbox.reset()
        self._fifo_mem.reset()
        self._txt_mem.reset()

    def _get_feedback(self, msg:str) -> str:
        print(colored("agent: ", "light_green", attrs=["bold"]), colored(msg, "light_green"))
        feedback = input(colored("> ", "light_blue", attrs=["bold"]))
        return feedback


    def _act(self, command:str, args:List[str]) -> Tuple[bool, Optional[Message]]:
        """
        Execute a command on the browser sandbox
        Returns a tuple of (completed, msg)
        """
        exception_msg = None

        # User interaction commands
        if command == "COMPLETE":
            return True, [Message(role="agent", content=args[0], timestamp=datetime.now())]
        elif command == "YIELD" or command == "ASK":
            msgs = [Message(role="agent", content=args[0], timestamp=datetime.now()),
                    Message(role="user", content=self._get_feedback(args[0]), timestamp=datetime.now())]
            return False, msgs

        if command == "TINSERT":
            try:
                self._txt_mem.insert(args[1], int(args[0]))
                return False, [Message(role="txt", content=f"Text inserted at line {args[0]}", timestamp=datetime.now())]
            except Exception as e:
                exception_msg = str(e)
                return False, [Message(role="txt", content=f"Action unsuccessful, an exception occured: {exception_msg}", timestamp=datetime.now())]
        elif command == "TREPLACE":
            try:
                self._txt_mem.replace(args[2], (int(args[0]), int(args[1])))
                return False, [Message(role="txt", content=f"Text replaced from line {args[0]} to line {args[1]}", timestamp=datetime.now())]
            except Exception as e:
                exception_msg = str(e)
                return False, [Message(role="txt", content=f"Action unsuccessful, an exception occured: {exception_msg}", timestamp=datetime.now())]

        # Browser-related commands
        if command == "VISIT":
            try:
                url = args[0]
                self._sandbox.visit(url)
            except Exception as e:
                exception_msg = str(e)
        elif command == "SUP":
            self._sandbox.scroll(scroll_down=False)
        elif command == "SDOWN":
            self._sandbox.scroll()
        elif command == "CLICK":
            try:
                index = int(args[0])
                self._sandbox.click(index)
            except Exception as e:
                exception_msg = str(e)
        elif command == "TYPE":
            try:
                index = int(args[0])
                text = args[1]
                self._sandbox.type(index, text)
            except Exception as e:
                exception_msg = str(e)
        elif command == "TYPESUBMIT":
            try:
                index = int(args[0])
                text = args[1]
                self._sandbox.type(index, text, enter=True)
            except Exception as e:
                exception_msg = str(e)
        elif command == "BACK":
            self._sandbox.go_back()
        elif command == "FORWARD":
            self._sandbox.go_forward()
        else:
            exception_msg = "Invalid command."
        
        current_url = self._sandbox.current_url
        scroll_percentage, scroll_y, scroll_height = self._sandbox.scroll_progress
        scroll_percentage = "{:3.2f}".format(scroll_percentage * 100)
        info_str = f"Current url: {current_url}\nCurrent scroll position: {scroll_percentage}% (scroll-y={scroll_y}, scroll-height={scroll_height})"

        if exception_msg is not None:
            return False, [Message(role="browser", content=f"Action unsuccessful, an exception occured: {exception_msg}\n" + info_str, timestamp=datetime.now())]
        return False, [Message(role="browser", content="Action successful!\n" + info_str, timestamp=datetime.now())]
    
    def run(self, objective, bg_info=None):
        self._fifo_mem.insert(Message(role="user", content=objective, timestamp=datetime.now()))
        completed = False

        while not completed:
            dom = self._sandbox.simplify_web_elements()
            if dom is None:
                dom = "[empty page]"
            print(dom)
            history = str(self._fifo_mem)
            user_prompt = USER_PROMPT.replace("$dom", dom).replace("$history", history).replace("$objective", objective).replace("$txt_file", str(self._txt_mem))

            reply = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {
                        "role": "system",
                        "content": SYSTEM_PROMPT,
                    },
                    {
                        "role": "user",
                        "content": user_prompt,
                    }
                ],
                response_model=_AgentReply,
            )
            # TODO: replace print statements with logging
            msg = Message(role="agent", content=reply.rationale, timestamp=datetime.now())
            self._fifo_mem.insert(msg)
            print(msg)

            command_content = f"{reply.command} {' '.join(reply.args)}"
            msg = Message(role="agent", content=command_content, timestamp=datetime.now())
            self._fifo_mem.insert(msg)
            print(msg)

            completed, browser_msgs = self._act(reply.command, reply.args)
            for browser_msg in browser_msgs:
                self._fifo_mem.insert(browser_msg)
                print(browser_msg)

if __name__ == "__main__":
    agent = ReActBrowserAgent()
    while True:
        agent.reset()
        print("Please enter your objective (type `exit` to exit): ")
        objective = input("> ")
        if objective == "exit":
            break
        agent.run(objective)
        print(agent._txt_mem)
        input("Press enter to continue...")