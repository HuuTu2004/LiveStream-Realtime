import time
import os
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from avatars.base_avatar import BaseAvatar
from utils.logger import logger

def llm_response(message,avatar_session:'BaseAvatar',datainfo:dict={}):
    try:
        opt = avatar_session.opt
        start = time.perf_counter()
        # Use configured LLM from CLI arguments
        from openai import OpenAI
        client = OpenAI(
            api_key="none", # Most local/remote providers don't need a key
            base_url=opt.llm_url,
        )
        
        completion = client.chat.completions.create(
            model=opt.llm_model,
            messages=[{'role': 'system', 'content': 'Bạn là một trợ lý bán hàng livestream chuyên nghiệp. Hãy trả lời câu hỏi của khách hàng thật ngắn gọn, lôi cuốn và tự nhiên bằng tiếng Việt.'},
                    {'role': 'user', 'content': message}],
            stream=True,
        )

        result=""
        first = True
        for chunk in completion:
            if len(chunk.choices)>0:
                #print(chunk.choices[0].delta.content)
                if first:
                    end = time.perf_counter()
                    logger.info(f"llm Time to first chunk: {end-start}s")
                    first = False
                msg = chunk.choices[0].delta.content
                if msg is None:
                    continue
                lastpos=0
                #msglist = re.split('[,.!;:，。！?]',msg)
                for i, char in enumerate(msg):
                    if char in ",.!;:，。！？：；" :
                        result = result+msg[lastpos:i+1]
                        lastpos = i+1
                        if len(result)>10:
                            logger.info(result)
                            avatar_session.put_msg_txt(result,datainfo)
                            result=""
                result = result+msg[lastpos:]
        end = time.perf_counter()
        logger.info(f"llm Time to last chunk: {end-start}s")
        if result:
            avatar_session.put_msg_txt(result,datainfo)
        
    except Exception as e:
        logger.exception('llm exceptiopn:')
        return   