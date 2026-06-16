import asyncio
import json
import time
from db.queries import create_conversation, get_last_2_path_of_user_speech
from llm.llm_utils import SKILL_GROUP_IDS, playback, validate_passport, initialize_mcp
from llm.mcp_tools import main, get_model_name, get_tools, client
from s3_storage import storage
from utils.utils import decode_mcp_text, SYSTEM_PROMPT_SOCIAL, gender_detection2
from dotenv import load_dotenv
from handover import trigger_handover

load_dotenv()

async def message_streaming(conversation=[], lan="uz", websocket=None, session_id=None, db_session_id=None):

    mcp_tools = await get_tools()
    print(f"✅ Tools loaded: {len(mcp_tools)}", flush=True)

    model = await get_model_name()
    print(f"✅ Model resolved: {model}", flush=True)

    if model is False:
        yield "Model yuklama ostida, Iltimos keyinroq urinib ko'ring"
        return

    print("Model load: ", client.base_url, model, flush=True)

    try:
        t = time.time()
        print("\n\nConversation history:", flush=True)
        print(conversation, flush=True)
        print("\n\n", flush=True)
        llm_stop = False

        while llm_stop is False:

            stream = await client.chat.completions.create(
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                model=model,
                messages=conversation,
                tools=mcp_tools,
                stream=True,
            )

            full_content = ""
            tool_calls_buffer = {}

            async for chunk in stream:
                delta = chunk.choices[0].delta
                if delta.content:
                    full_content += delta.content
                    yield decode_mcp_text(delta.content)

                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in tool_calls_buffer:
                            tool_calls_buffer[idx] = {
                                "id": tc_delta.id,
                                "name": tc_delta.function.name,
                                "arguments": ""
                            }
                        if tc_delta.function.arguments:
                            tool_calls_buffer[idx]["arguments"] += tc_delta.function.arguments

            formatted_tool_calls = []
            for idx in sorted(tool_calls_buffer.keys()):
                tc = tool_calls_buffer[idx]
                formatted_tool_calls.append({
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": tc["arguments"]
                    }
                })

            history_entry = {"role": "assistant"}
            if full_content:
                history_entry["content"] = full_content
            if formatted_tool_calls:
                history_entry["tool_calls"] = formatted_tool_calls

            conversation.append(history_entry)

            if not formatted_tool_calls:
                return

            for tc in formatted_tool_calls:
                tool_name = tc["function"]["name"]
                tool_args = json.loads(tc["function"]["arguments"])
                _current_session_id = session_id

                print("Tool name: ", tool_name, flush=True)

                try:
                    if tool_name == "end_call":
                        print("🚪 End call tool triggered. Closing websocket...")
                        await playback(websocket, "ivr_call_end_16k.wav")
                        await websocket.send("__close__")
                        await asyncio.sleep(0.1)
                        await websocket.close(1000, "End Call Tool Triggered")
                        llm_stop = True
                        return

                    if tool_name == "operator_call":
                        ivr_number = tool_args.get("ivr_number", "5")
                        target_skill_group_id = SKILL_GROUP_IDS.get(str(ivr_number), SKILL_GROUP_IDS["5"])
                        print(f"operator_call → IVR {ivr_number}, skill_group: {target_skill_group_id}, session: {_current_session_id}", flush=True)

                        # 1. Avval audio to'liq o'ynatiladi (mijoz eshitadi)
                        await playback(websocket, "ivr_" + ivr_number + "_16k.wav")

                        # 2. Audio tugagach handover bajariladi (boshqa IVR ga o'tadi)
                        if _current_session_id and target_skill_group_id:
                            success = await trigger_handover(_current_session_id, target_skill_group_id)
                            print(f"Handover result: {success} for _current_session_id: {_current_session_id}, target_skill_group_id: {target_skill_group_id}", flush=True)
                            holat_matni = f"Mijozni {ivr_number} raqamli ivrga yo'naltirish, {'Muvoffaqiyatli✅' if success else 'Xatolik yuz berdi❌'}"
                            print(holat_matni)
                            await asyncio.to_thread(
                                create_conversation, role="assistant", message=holat_matni, speech_path="", session_id=db_session_id
                            )
                        else:
                            print(f"Warning: session_id yoki skill_group_id yo'q, handover bajarilmadi. {_current_session_id}, {target_skill_group_id}", flush=True)                            

                        await asyncio.sleep(0.1)
                        llm_stop = True

                        # if _current_session_id and target_skill_group_id:
                        #     success = await trigger_handover(_current_session_id, target_skill_group_id)
                        #     print(f"Handover second try result: {success} for _current_session_id: {_current_session_id}, target_skill_group_id: {target_skill_group_id}", flush=True)
                        # else:
                        #     print(f"Warning: session_id yoki skill_group_id yo'q, handover bajarilmadi2. {_current_session_id}, {target_skill_group_id}", flush=True)

                        return

                    if tool_name == "check_ihma_tool":
                        print("Validating passport format for check_ihma_tool... Arg", tool_args, flush=True)
                        passport_raw = tool_args.get("passport", "")
                        valid = validate_passport(passport_raw)

                        if valid is None:
                            conversation.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": tool_name,
                                "content": json.dumps({
                                    "error": "INVALID_PASSPORT_FORMAT",
                                    "passport_received": passport_raw,
                                    "message": f"Passport '{passport_raw}' noto'g'ri formatda. 2 ta harf va aynan 7 ta raqam bo'lishi kerak. Foydalanuvchidan qayta so'rang."
                                })
                            })
                            continue
                        else:
                            await playback(websocket, "hmaq_16k.wav")#hurmatli mijoz aloqada qoling

                        paths = get_last_2_path_of_user_speech(db_session_id)
                        
                        a = await storage.get_object(str(paths[0][0]))
                        a2 = await storage.get_object(str(paths[1][0]))

                        birinchi_5_sekund = a[:160000]
                        ikkinchi_5_sekund = a2[:160000]

                        count_system = sum(1 for msg in conversation if msg["role"] == "system")
                        if count_system == 1:
                            gender, confidence = await gender_detection2([birinchi_5_sekund, ikkinchi_5_sekund])
                            if confidence and confidence >= 0.5:
                                conversation.append({"role": "system", "content": f"User gender is {gender}. Adjust tone accordingly."})
                                        
                    if tool_name in ("pension_get_payment_region_district_street"):
                        await playback(websocket, "hmh1s_16k.wav") # hurmatli mijoz bir soniya




                    result = await main.call_tool(tool_name, tool_args)
                    print(f"Tool '{tool_name}' executed. Result: {result}", flush=True)
                    content = "".join([item.text if item.type == "text" else "" for item in result.content])

                    conversation.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": tool_name,
                        "content": content
                    })
                except Exception as e:
                    conversation.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": tool_name,
                        "content": f"Error: {e}"
                    })
    finally:
        print("LLM response stream ended. ", time.time() - t, " seconds.", flush=True)




# async def console_chat():
#     await initialize_mcp()
    
#     # conv = [
#     #     {"role": "system", "content": SYSTEM_PROMPT_SOCIAL},
        
#     # ]
 

#     conv =  [{'role': 'system', 'content': SYSTEM_PROMPT_SOCIAL}
#              ]

#     print("🤖 Chat tayyor! Chiqish uchun 'exit' yozing.\n")

#     while True:
#         user_input = input("Siz: ").strip()

#         if not user_input:
#             continue
#         if user_input.lower() in ("exit", "quit", "chiq"):
#             print("Xayр!\n\n")
#             print(conv)
#             print("\n\nChat yakunlandi.")
#             break

#         conv.append({"role": "user", "content": user_input})

#         print("Bot: ", end="", flush=True)

#         full_response = ""
#         async for chunk in message_streaming(
#             conversation=conv,
#             lan="uz",
#             websocket=None   # console da websocket yo'q
#         ):
#             print(chunk, end="", flush=True)
#             full_response += chunk

#         print()  # yangi qator


# if __name__ == "__main__":
#     asyncio.run(console_chat())