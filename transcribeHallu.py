import sys
import os
import time
import re
from _io import StringIO
import json
from json_util import split_transcription, convert_gladia_to_internal_format

if sys.version_info.major == 3 and sys.version_info.minor >= 10:
    print("Python >= 3.10")
    import collections.abc
    import collections
    collections.MutableMapping = collections.abc.MutableMapping
else:
    print("Python < 3.10")
    import collections
    
import traceback

import torch

torch.set_num_threads(1)
useSileroVAD=True
if(useSileroVAD):
    modelVAD, utils = torch.hub.load(repo_or_dir='snakers4/silero-vad',
                              model='silero_vad',
                              force_reload=False,
                              onnx=False)
    (get_speech_timestamps,
     save_audio,
     read_audio,
     VADIterator,
     collect_chunks) = utils

useSpleeter=False
if(useSpleeter):
    from spleeter.audio import STFTBackend
    backend = STFTBackend.LIBROSA
    from spleeter.separator import Separator
    print("Using spleeter:2stems-16kHz")
    separator = Separator('spleeter:2stems-16kHz',stft_backend=backend)

useDemucs=True
if(useDemucs):
    from demucsWrapper import load_demucs_model
    from demucsWrapper import demucs_audio
    print("Using Demucs")
    modelDemucs = load_demucs_model()

useCompressor=True

try:
    #Standard Whisper: https://github.com/openai/whisper
    import whisper
    print("Using standard Whisper")
    whisperFound = "STD"
    from pathlib import Path
    from whisper.utils import WriteSRT
except ImportError as e:
    pass

try:
    #FasterWhisper: https://github.com/guillaumekln/faster-whisper
    from faster_whisper import WhisperModel
    print("Using Faster Whisper")
    whisperFound = "FSTR"
    modelPath = "whisper-medium-ct2/"#"whisper-medium-ct2/" "whisper-large-ct2/"
    if not os.path.exists(modelPath):
        print("Faster installation found, but "+modelPath+" model not found")
        sys.exit(-1)
except ImportError as e:
    pass

try:
    from seamless_communication.models.inference import Translator
    from lang2to3 import lang2to3
    lang2to3 = lang2to3()
    whisperFound = "SM4T"
except ImportError as e:
    pass

#large-v3 model seems to be bad with music, thus keep v2 as the default
whisperVersion = "-v2" #May be "", "-V1", "-v2, "-v3"
whisperLoaded = "??"
beam_size=5
patience=0
temperature=0
model = None
device = "cuda" #cuda / cpu
cudaIdx = 0

SAMPLING_RATE = 16000
MAX_DURATION = 600
TRUNC_DURATION = MAX_DURATION

from threading import Lock, Thread
lock = Lock()

def loadModel(gpu: str,modelSize=None):
    global model
    global device
    global cudaIdx
    global whisperLoaded
    cudaIdx = gpu
    try:
        if whisperFound == "FSTR":
            if(modelSize == "large"):
                modelPath = "whisper-large-ct2/"
            else:
                modelPath = "whisper-medium-ct2/"
            print("LOADING: "+modelPath+" GPU: "+gpu+" BS: "+str(beam_size)+" PTC="+str(patience)+" TEMP="+str(temperature))
            compute_type="float16"# float16 int8_float16 int8
            model = WhisperModel(modelPath, device=device,device_index=int(gpu), compute_type=compute_type)
        elif whisperFound == "STD":
            if(modelSize == None):
                modelSize="medium"#"tiny"#"medium" #"large"
            if(modelSize == "large"):
                modelSize = "large"+whisperVersion #"large-v1" "large-v2" "large-v3"
            print("LOADING: "+modelSize+" GPU:"+gpu+" BS: "+str(beam_size)+" PTC="+str(patience)+" TEMP="+str(temperature))
            model = whisper.load_model(modelSize,device=torch.device("cuda:"+gpu)) #May be "cpu"
        elif whisperFound == "SM4T":
            print("LOADING: "+"seamlessM4T_large"+" GPU:"+gpu)
            model = Translator("seamlessM4T_large", "vocoder_36langs", torch.device("cuda:"+gpu), torch.float16)
        print("LOADED")
        whisperLoaded = modelSize
    except Exception as e:
        print("Can't load Whisper model: "+whisperFound+"/"+modelSize)
        print(e)
        sys.exit(-1)

def loadedModel():
    return whisperFound+" "+whisperLoaded

def getDuration(aLog:str):
    duration = None
    time = None
    with open(aLog) as f:
        lines = f.readlines()
        for line in lines:
            if(re.match(r"^ *Duration: [0-9][0-9]:[0-9][0-9]:[0-9][0-9][.][0-9][0-9], .*$", line, re.IGNORECASE)):
                duration = re.sub(r"(.*Duration: *|[,. ].*)", "", line, re.IGNORECASE)
                return sum(x * int(t) for x, t in zip([3600, 60, 1], duration.split(":")))
            for aSub in line.split("[\r\n]"):
                if(re.match(r"^.*time=[0-9][0-9]:[0-9][0-9]:[0-9][0-9][.][0-9][0-9] .*$", aSub, re.IGNORECASE)):
                    #print("SUB="+aSub)
                    time = re.sub(r"(.*time=|[,. ].*)", "", aSub, re.IGNORECASE)
    #Return last found time value
    if(time != "00:00:00"):
        print("TIME="+str(time))
        return sum(x * int(t) for x, t in zip([3600, 60, 1], time.split(":")))
    return None

def formatTimeStamp(aT=0):
    aH = int(aT/3600)
    aM = int((aT%3600)/60)
    aS = (aT%60)
    return "%02d:%02d:%06.3f" % (aH,aM,aS)

def getPrompt(lng:str):
    if(lng == "en"):
        aOk=""
        return "Whisper, Ok. "\
            +"A pertinent sentence for your purpose in your language. "\
            +"Ok, Whisper. Whisper, Ok. Ok, Whisper. Whisper, Ok. "\
            +"Please find here, an unlikely ordinary sentence. "\
            +"This is to avoid a repetition to be deleted. "\
            +"Ok, Whisper. "
    
    if(lng == "fr"):
        return "Whisper, Ok. "\
            +"Une phrase pertinente pour votre propos dans votre langue. "\
            +"Ok, Whisper. Whisper, Ok. Ok, Whisper. Whisper, Ok. "\
            +"Merci de trouver ci-joint, une phrase ordinaire improbable. "\
            +"Pour éviter une répétition à être supprimée. "\
            +"Ok, Whisper. "
    
    if(lng == "uk"):
        return "Whisper, Ok. "\
            +"Доречне речення вашою мовою для вашої мети. "\
            +"Ok, Whisper. Whisper, Ok. Ok, Whisper. Whisper, Ok. "\
            +"Будь ласка, знайдіть тут навряд чи звичайне речення. "\
            +"Це зроблено для того, щоб уникнути повторення, яке потрібно видалити. "\
            +"Ok, Whisper. "
    
    if(lng == "hi"):
        return "विस्पर, ओके. "\
            +"आपकी भाषा में आपके उद्देश्य के लिए एक प्रासंगिक वाक्य। "\
            +"ओके, विस्पर. विस्पर, ओके. ओके, विस्पर. विस्पर, ओके. "\
            +"कृपया यहां खोजें, एक असंभावित सामान्य वाक्य। "\
            +"यह हटाए जाने की पुनरावृत्ति से बचने के लिए है। "\
            +"ओके, विस्पर. "
    
    #Not Already defined?
    return ""

def transcribePrompt(path: str, lng: str, prompt=None, lngInput=None, isMusic=False, addSRT=False, truncDuration=TRUNC_DURATION, maxDuration=MAX_DURATION):
    """Whisper transcribe with language detection and Gladia API for non-English."""

    if lngInput is None:
        lngInput = lng
        print("Using output language as input language: " + lngInput)
    
    if prompt is None:
        if not isMusic:
            prompt = getPrompt(lng)
        else:
            prompt = ""
    
    print("=====transcribePrompt", flush=True)
    print("PATH=" + path, flush=True)
    print("LNGINPUT=" + lngInput, flush=True)
    print("LNG=" + lng, flush=True)
    print("PROMPT=" + prompt, flush=True)
    
    opts = dict(language=lng, initial_prompt=prompt, word_timestamps=True)
    return transcribeOpts(path, opts, lngInput, isMusic=isMusic, addSRT=addSRT, subEnd=truncDuration, maxDuration=maxDuration)

def count_weird_words(text):
    return text.count("Hãy đăng ký kênh") + text.count("subscribe cho")

def transcribeOpts(path: str, opts: dict, lngInput=None, isMusic=False, onlySRT=False, addSRT=False, subBeg="0", subEnd=str(TRUNC_DURATION), maxDuration=MAX_DURATION, stretch=None, nbRun=1, remixFactor="0.3", speechnorm=True, max_line_width=80, max_line_count=2):
    pathIn = path
    pathClean = path
    pathNoCut = path
    
    initTime = time.time()
    
    startTime = time.time()
    duration = -1
    try:
        #Convert to WAV to avoid later possible decoding problem
        pathWAV = pathIn+".WAV"+".wav"
        aCmd = "ffmpeg -y"+" -i \""+pathIn+"\""+" -ss "+subBeg+" -to "+subEnd + " -c:a pcm_s16le -ar "+str(SAMPLING_RATE)+" \""+pathWAV+"\" > \""+pathWAV+".log\" 2>&1"
        print("CMD: "+aCmd)
        os.system(aCmd)
        duration = getDuration(pathWAV+".log")
        print("T=",(time.time()-startTime))
        print("DURATION="+str(duration)+" subBeg="+str(subBeg)+" subEnd="+str(subEnd))
        print("PATH="+pathWAV,flush=True)
        pathIn = pathClean = pathWAV
    except Exception as e:
         print("Warning: can't convert to WAV")
         print(e)

    try:
        if(stretch != None):
            pathSTRETCH = pathIn+".STRETCH"+".wav"
            #ffmpeg STRECH
            aCmd = "ffmpeg -y -i \""+pathIn+"\""+" -t "+str(truncDuration) + " -filter:a \"atempo="+stretch+"\"" + " -c:a pcm_s16le -ar "+str(SAMPLING_RATE)+" \""+pathSTRETCH+"\" > \""+pathSTRETCH+".log\" 2>&1"
            #sox STRECH
            #aCmd = "sox \""+pathIn+"\""+" \""+pathSTRETCH+"\" tempo "+stretch+" > \""+pathSTRETCH+".log\" 2>&1"
            #soundstretch STRECH
            #aCmd = "soundstretch \""+pathIn+"\""+" \""+pathSTRETCH+"\" -tempo="+str(int(100*float(stretch)) - 100)+" > \""+pathSTRETCH+".log\" 2>&1"
            #rubberband STRECH
            #aCmd = "rubberband \""+pathIn+"\""+" \""+pathSTRETCH+"\" --tempo "+stretch+" > \""+pathSTRETCH+".log\" 2>&1"
            print("CMD: "+aCmd)
            os.system(aCmd)
            print("T=",(time.time()-startTime))
            print("PATH="+pathWAV,flush=True)
            pathIn = pathClean = pathWAV = pathSTRETCH
    except Exception as e:
         print("Warning: can't STRETCH")
         print(e)

    startTime = time.time()
    try:
        #Check for duration
        aCmd = "ffmpeg -y -i \""+pathIn+"\" "+ " -f null - > \""+pathIn+".dur\" 2>&1"
        print("CMD: "+aCmd)
        os.system(aCmd)
        print("T=",(time.time()-startTime))
        duration = getDuration(pathIn+".dur")
        print("DURATION="+str(duration)+" max "+str(maxDuration))
        if(duration > maxDuration):
            return "[Too long ("+str(duration)+"s)]"
    except Exception as e:
         print("Warning: can't analyze duration")
         print(e)

    try:
        if(useSpleeter):
            startTime = time.time()
            spleeterDir=pathIn+".spleeter"
            if(not os.path.exists(spleeterDir)):
                os.mkdir(spleeterDir)
            pathSpleeter=spleeterDir+"/"+os.path.splitext(os.path.basename(pathIn))[0]+"/vocals.wav"
            separator.separate_to_file(pathIn, spleeterDir)
            print("T=",(time.time()-startTime))
            print("PATH="+pathSpleeter,flush=True)
            pathNoCut = pathIn = pathSpleeter
    except Exception as e:
         print("Warning: can't split vocals")
         print(e)
    
    if(useDemucs):
        startTime = time.time()
        try:
            #demucsDir=pathIn+".demucs"
            #if(not os.path.exists(demucsDir)):
            #    os.mkdir(demucsDir)
            pathDemucsVocals=pathIn+".vocals.wav" #demucsDir+"/htdemucs/"+os.path.splitext(os.path.basename(pathIn))[0]+"/vocals.wav"
            pathDemucsDrums=pathIn+".drums.wav"
            pathDemucsBass=pathIn+".bass.wav"
            pathDemucsOther=pathIn+".other.wav"
            #Demucs seems complex, using CLI cmd for now
            #aCmd = "python -m demucs --two-stems=vocals -d "+device+":"+cudaIdx+" --out "+demucsDir+" "+pathIn
            #print("CMD: "+aCmd)
            #os.system(aCmd)
            demucs_audio(pathIn=pathIn,model=modelDemucs,device="cuda:"+cudaIdx,pathVocals=pathDemucsVocals,pathOther=pathIn+".other.wav")
            print("T=",(time.time()-startTime))
            print("PATH="+pathDemucsVocals,flush=True)
            pathNoCut = pathIn = pathDemucsVocals
        except Exception as e:
             print("Warning: can't split vocals")
             print(e)

    startTime = time.time()
    try:
        pathSILCUT = pathIn+".SILCUT"+".wav"
        aCmd = "ffmpeg -y -i \""+pathIn+"\" -af \"silenceremove=start_periods=1:stop_periods=-1:start_threshold=-50dB:stop_threshold=-50dB:start_silence=0.2:stop_silence=0.2, loudnorm\" "+ " -c:a pcm_s16le -ar "+str(SAMPLING_RATE)+" \""+pathSILCUT+"\" > \""+pathSILCUT+".log\" 2>&1"
        print("CMD: "+aCmd)
        os.system(aCmd)
        print("T=",(time.time()-startTime))
        print("PATH="+pathSILCUT,flush=True)
        pathIn = pathSILCUT
    except Exception as e:
         print("Warning: can't filter blanks")
         print(e)
    
    try:
        if(not isMusic and useSileroVAD):
            startTime = time.time()
            
            pathVAD = pathIn+".VAD.wav"
            wav = read_audio(pathIn, sampling_rate=SAMPLING_RATE)
            #https://github.com/snakers4/silero-vad/blob/master/utils_vad.py#L161
            speech_timestamps = get_speech_timestamps(wav, modelVAD,threshold=0.5,min_silence_duration_ms=500, sampling_rate=SAMPLING_RATE)
            save_audio(pathVAD,collect_chunks(speech_timestamps, wav), sampling_rate=SAMPLING_RATE)
            print("T=",(time.time()-startTime))
            print("PATH="+pathVAD,flush=True)
            pathIn = pathVAD
    except Exception as e:
         print("Warning: can't filter noises")
         print(e)

    try:
        if(float(remixFactor) >= 1):
            pathREMIXN = pathClean
        elif (float(remixFactor) <= 0 and useDemucs):
            pathREMIXN = pathDemucsVocals;
        elif (isMusic and useDemucs):
            startTime = time.time()
            
            if(speechnorm):
                pathNORM = pathDemucsVocals+".NORM.wav"
                aCmd = ("ffmpeg -y -i \""+pathDemucsVocals+"\""
                        #+ " -filter:a loudnorm"
                        +" -af \"speechnorm=e=50:r=0.0005:l=1\""
                        +" \""+pathNORM+"\" > \""+pathNORM+".log\" 2>&1")
                print("CMD: "+aCmd)
                os.system(aCmd)
                print("T=",(time.time()-startTime))
                print("PATH="+pathNORM,flush=True)
            else:
                pathNORM = pathDemucsVocals

            pathREMIXN = pathNORM+".REMIX.wav"
            aCmd = ("ffmpeg -y -i \""+pathNORM+"\" -i \""+pathDemucsDrums+"\" -i \""+pathDemucsBass+"\" -i \""+pathDemucsOther+"\""
                    +" -filter_complex amix=inputs=4:duration=longest:dropout_transition=0:weights=\"1 "+remixFactor+" "+remixFactor+" "+remixFactor+"\""
                    +" \""+pathREMIXN+"\" > \""+pathREMIXN+".log\" 2>&1")
            print("CMD: "+aCmd)
            os.system(aCmd)
            print("T=",(time.time()-startTime))
            print("PATH="+pathREMIXN,flush=True)
    except Exception as e:
         print("Warning: can't remix")
         print(e)

    mode=1
    if(duration > 30):
        print("NOT USING MARKS FOR DURATION > 30s")
        mode=0
    
    startTime = time.time()
    if(onlySRT):
        result = {}
        result["text"] = ""
    else:
        result = transcribeMARK(pathIn, opts, mode=mode, lngInput=lngInput, isMusic=isMusic,
                                nbRun=nbRun, max_line_width=max_line_width, max_line_count=max_line_count)
        if len(result["text"]) <= 0:
            result["text"] = "--"
    
    if(onlySRT or addSRT):
        #Better timestamps using original music clip
        if(isMusic
               #V3 is very bad with music!?
               and not whisperVersion == "-v3"
               ):
            if(pathREMIXN is not None):
                resultSRT = transcribeMARK(pathREMIXN, opts, mode=3, lngInput=lngInput, isMusic=isMusic,
                                           nbRun=nbRun, max_line_width=max_line_width, max_line_count=max_line_count)
                
                weird_word_count_1 = count_weird_words(resultSRT["srt"])
                weird_word_count_threshold = 2
                # special case for Vietnamese
                if lngInput.lower() == 'vi' and weird_word_count_1 > weird_word_count_threshold:
                    print("Vietnamese special case")
                    print("weird_word_count_1 = ", weird_word_count_1)
                    resultSRT2 = transcribeMARK(pathNoCut, opts, mode=3, lngInput=lngInput, isMusic=isMusic,
                                               nbRun=nbRun, max_line_width=max_line_width, max_line_count=max_line_count)   
                    weird_word_count_2 = count_weird_words(resultSRT2["srt"])
                    print("weird_word_count_2 = ", weird_word_count_2)
                    if weird_word_count_2 < weird_word_count_1:
                        resultSRT = resultSRT2
                    if weird_word_count_2 > weird_word_count_threshold:
                        if "SILCUT" not in pathIn:
                            resultSRT3 = transcribeMARK(pathIn, opts, mode=3, lngInput=lngInput, isMusic=isMusic,
                                                nbRun=nbRun, max_line_width=max_line_width, max_line_count=max_line_count)
                        else:
                            resultSRT3 = resultSRT2
                        
                        weird_word_count_3 = count_weird_words(resultSRT3["srt"])
                        print("weird_word_count_3 = ", weird_word_count_3)
                        if weird_word_count_3 < weird_word_count_2:
                            resultSRT = resultSRT3
                        if weird_word_count_3 > weird_word_count_threshold:
                            if "SILCUT" not in pathIn:
                                resultSRT4 = transcribe_with_gladia(pathIn, lngInput, opts["language"])
                            else:
                                resultSRT4 = transcribe_with_gladia(pathREMIXN, lngInput, opts["language"])
                            
                            resultSRT4 = json.loads(resultSRT4)
                            weird_word_count_4 = count_weird_words(resultSRT4["text"])
                            print("weird_word_count_4 = ", weird_word_count_4)
                            if weird_word_count_4 < weird_word_count_3:
                                resultSRT = resultSRT4
                            if weird_word_count_4 > weird_word_count_threshold:
                                resultSRT5 = transcribeMARK(pathClean, opts, mode=3, lngInput=lngInput, isMusic=isMusic,
                                            nbRun=nbRun, max_line_width=max_line_width, max_line_count=max_line_count)
                                weird_word_count_5 = count_weird_words(resultSRT5["srt"])
                                print("weird_word_count_5 = ", weird_word_count_5)
                                if weird_word_count_5 < weird_word_count_4:
                                    resultSRT = resultSRT5
            else:
                resultSRT = transcribeMARK(pathClean, opts, mode=3, lngInput=lngInput, isMusic=isMusic,
                                           nbRun=nbRun, max_line_width=max_line_width, max_line_count=max_line_count)
        else:
            resultSRT = transcribeMARK(pathNoCut, opts, mode=3, lngInput=lngInput, isMusic=isMusic,
                                       nbRun=nbRun, max_line_width=max_line_width, max_line_count=max_line_count)
        # Ensure resultSRT is a dictionary before accessing its keys
        if isinstance(resultSRT, dict):
            result = {
                "srt": resultSRT.get("srt", ""),
                "text": resultSRT.get("text", ""),
                "json": resultSRT.get("json", [])
            }
        else:
            print(f"Warning: resultSRT is not a dictionary. Type: {type(resultSRT)}")
            print("resultSRT = ", resultSRT)
            result = {
                "srt": "",
                "text": resultSRT.get("text", ""),
                "json": []
            }
    else:
        result = {
            "srt": "",
            "text": result.get("text", ""),
            "json": result.get("json", [])
        }
  
    result["json"] = split_transcription(result["json"])
    

    print("T=",(time.time()-initTime))
    if(len(result["text"]) > 0):
        print("s/c=",(time.time()-initTime)/len(result["text"]))
    print("c/s=",len(result["text"])/(time.time()-initTime))
    
    return json.dumps(result)

def transcribeMARK(path: str, opts: dict, mode=1, lngInput=None, aLast=None, isMusic=False, nbRun=1, max_line_width=80, max_line_count=2):
    print("transcribeMARK(): "+path)
    pathIn = path
    
    lng = opts["language"]
    
    if(lngInput == None):
        lngInput = lng
        
    noMarkRE = "^(ar|he|ru|zh)$"
    if(lng != None and re.match(noMarkRE,lng) and mode != 3):
        #Need special voice marks
        mode = 0
    
    if(isMusic and mode != 3):
        #Markers are not really interesting with music
        mode = 0
    
    if(whisperFound == "SM4T"):
        #Not marker with SM4T
        mode = 0
    
    if os.path.exists("markers/WOK-MRK-"+lngInput+".wav"):
        mark1="markers/WOK-MRK-"+lngInput+".wav"
    else:
        mark1="markers/WOK-MRK.wav"
    if os.path.exists("markers/OKW-MRK-"+lngInput+".wav"):
        mark2="markers/OKW-MRK-"+lngInput+".wav"
    else:
        mark2="markers/OKW-MRK.wav"
    
    if(mode == 2):
        mark = mark1
        mark1 = mark2
        mark2 = mark
        
    if(mode == 0):
        print("["+str(mode)+"] PATH="+pathIn,flush=True)
    else:
        try:
            if(mode != 3):
                startTime = time.time()
                pathMRK = pathIn+".MRK"+".wav"
                aCmd = "ffmpeg -y -i "+mark1+" -i \""+pathIn+"\" -i "+mark2+" -filter_complex \"[0:a][1:a][2:a]concat=n=3:v=0:a=1[a]\" -map \"[a]\" -c:a pcm_s16le -ar "+str(SAMPLING_RATE)+" \""+pathMRK+"\" > \""+pathMRK+".log\" 2>&1"
                print("CMD: "+aCmd)
                os.system(aCmd)
                print("T=",(time.time()-startTime))
                print("["+str(mode)+"] PATH="+pathMRK,flush=True)
                pathIn = pathMRK
            
            if(useCompressor
                and not isMusic
                ):
                startTime = time.time()
                pathCPS = pathIn+".CPS"+".wav"
                aCmd = "ffmpeg -y -i \""+pathIn+"\" -af \"speechnorm=e=50:r=0.0005:l=1\" "+ " -c:a pcm_s16le -ar "+str(SAMPLING_RATE)+" \""+pathCPS+"\" > \""+pathCPS+".log\" 2>&1"
                print("CMD: "+aCmd)
                os.system(aCmd)
                print("T=",(time.time()-startTime))
                print("["+str(mode)+"] PATH="+pathCPS,flush=True)
                pathIn = pathCPS
        except Exception as e:
             print("Warning: can't add markers")
             print(e)
    
    def format_srt_text(text, max_width, max_lines):
        words = text.split()
        lines = []
        current_line = []
        current_length = 0

        for word in words:
            if current_length + len(word) + 1 > max_width:
                lines.append(' '.join(current_line))
                current_line = [word]
                current_length = len(word)
            else:
                current_line.append(word)
                current_length += len(word) + 1

        if current_line:
            lines.append(' '.join(current_line))

        return '\n'.join(lines[:max_lines])

    startTime = time.time()
    lock.acquire()
    try:
        transcribe_options = dict(**opts)  # avoid adding beam_size opt several times
        if beam_size > 1:
            transcribe_options["beam_size"] = beam_size
        if patience > 0:
            transcribe_options["patience"] = patience
        if temperature > 0:
            transcribe_options["temperature"] = temperature

        # Check if both input and target languages are non-English and different
        # if lngInput and lng and lngInput.lower() == 'vi' :
        #     # Use Gladia API directly
        #     gladia_result = transcribe_with_gladia(pathIn, lngInput, lng)
        #     result = json.loads(gladia_result)
        # elif whisperFound == "FSTR":
        if whisperFound == "FSTR":
            result = {"text": "", "srt": "", "json": []}
            multiRes = ""
            for r in range(nbRun):
                print("RUN: "+str(r))
                segments, info = model.transcribe(pathIn,**transcribe_options)
                resSegs = []
                json_segments = []
                if(mode == 3):
                    aSegCount = 0
                    for segment in segments:
                        aSegCount += 1
                        formatted_text = format_srt_text(segment.text.strip(), max_line_width, max_line_count)
                        srt_entry = f"{aSegCount}\n{formatTimeStamp(segment.start)} --> {formatTimeStamp(segment.end)}\n{formatted_text}\n\n"
                        resSegs.append(srt_entry)
                        json_segment = {
                            "start": segment.start,
                            "end": segment.end,
                            "sentence": segment.text.strip(),
                            "words": []
                        }
                        if "word_timestamps" in transcribe_options:
                            for word in segment.words:
                                json_segment["words"].append({
                                    "start": word.start,
                                    "end": word.end,
                                    "text": word.word.strip()
                                })
                        json_segments.append(json_segment)
                else:
                    for segment in segments:
                        resSegs.append(segment.text)
                        json_segment = {
                            "start": segment.start,
                            "end": segment.end,
                            "sentence": segment.text.strip(),
                            "words": []
                        }
                        if "word_timestamps" in transcribe_options:
                            for word in segment.words:
                                json_segment["words"].append({
                                    "start": word.start,
                                    "end": word.end,
                                    "text": word.word.strip()
                                })
                        json_segments.append(json_segment)
                
                result["text"] += "".join(resSegs)
                result["srt"] += "".join(resSegs) if mode == 3 else ""
                result["json"].extend(json_segments)
                if(r > 0):
                    multiRes += "=====\n"
                multiRes += result["text"]
            
            if(nbRun > 1):
                result["text"] = multiRes
        elif whisperFound == "SM4T":
            src_lang = lang2to3[lngInput];
            tgt_lang = lang2to3[lng];
            # S2TT
            #translated_text, _, _ = translator.predict(<path_to_input_audio>, "s2tt", <tgt_lang>)
            translated_text, _, _ = model.predict(pathIn, "s2tt", tgt_lang)
            result = {
                "text": str(translated_text),
                "srt": "",
                "json": []
            }
        else:
            transcribe_options = dict(task="transcribe", **transcribe_options)
            multiRes = ""
            result = {"text": "", "srt": "", "json": []}
            for r in range(nbRun):
                print("RUN: "+str(r))
                whisper_result = model.transcribe(pathIn, **transcribe_options)
                if(mode == 3):
                    srt_segments = []
                    for i, segment in enumerate(whisper_result["segments"], start=1):
                        formatted_text = format_srt_text(segment['text'].strip(), max_line_width, max_line_count)
                        srt_entry = f"{i}\n{formatTimeStamp(segment['start'])} --> {formatTimeStamp(segment['end'])}\n{formatted_text}\n\n"
                        srt_segments.append(srt_entry)
                    result["srt"] += "".join(srt_segments)
                
                result["text"] += whisper_result["text"]
                for segment in whisper_result["segments"]:
                    json_segment = {
                        "start": segment["start"],
                        "end": segment["end"],
                        "sentence": segment["text"].strip(),
                        "words": [{"start": word["start"], "end": word["end"], "text": word["word"]} for word in segment.get("words", [])]
                    }
                    result["json"].append(json_segment)
                
                if(r > 0):
                    multiRes += "=====\n"
                multiRes += result["text"]
            
            if(nbRun > 1):
                result["text"] = multiRes
        
        print("T=",(time.time()-startTime))
        print("TRANS="+result["text"],flush=True)
    except Exception as e: 
        print(e)
        traceback.print_exc()
        lock.release()
        result = {"text": "", "srt": "", "json": []}
    
    lock.release()
    
    if(mode == 0 or mode == 3):
        return result
        #Too restrictive
        #if(result["text"] == aLast):
        #    #Only if confirmed
        #    return result
        #result["text"] = ""
        #return result
    
    aWhisper="(Whisper|Wisper|Wyspę|Wysper|Wispa|Уіспер|Ου ίσπερ|위스퍼드|ウィスパー|विस्पर|विसपर)"
    aOk="(o[.]?k[.]?|okay|oké|okej|Окей|οκέι|окэй|オーケー|ओके)"
    aSep="[.,!? ]*"
    if(mode == 1):
        aCleaned = re.sub(r"(^ *"+aWhisper+aSep+aOk+aSep+"|"+aOk+aSep+aWhisper+aSep+" *$)", "", result["text"], 2, re.IGNORECASE)
        if(re.match(r"^ *("+aOk+"|"+aSep+"|"+aWhisper+")*"+aWhisper+"("+aOk+"|"+aSep+"|"+aWhisper+")* *$", result["text"], re.IGNORECASE)):
            #Empty sound ?
            return transcribeMARK(path, opts, mode=2,lngInput=lngInput,aLast="")
        
        if(re.match(r"^ *"+aWhisper+aSep+aOk+aSep+".*"+aOk+aSep+aWhisper+aSep+" *$", result["text"], re.IGNORECASE)):
            #GOOD!
            result["text"] = aCleaned
            return result
        
        return transcribeMARK(path, opts, mode=2,lngInput=lngInput,aLast=aCleaned)
    
    if(mode == 2):
        aCleaned = re.sub(r"(^ *"+aOk+aSep+aWhisper+aSep+"|"+aWhisper+aSep+aOk+aSep+" *$)", "", result["text"], 2, re.IGNORECASE)
        if(aCleaned == aLast):
            #CONFIRMED!
            result["text"] = aCleaned
            return result
            
        if(re.match(r"^ *("+aOk+"|"+aSep+"|"+aWhisper+")*"+aWhisper+"("+aOk+"|"+aSep+"|"+aWhisper+")* *$", result["text"], re.IGNORECASE)):
            #Empty sound ? 
            result["text"] = ""
            return result
        
        if(re.match(r"^ *"+aOk+aSep+aWhisper+aSep+".*"+aWhisper+aSep+aOk+aSep+" *$", result["text"], re.IGNORECASE)):
            #GOOD!
            result["text"] = aCleaned
            return result
        
        return transcribeMARK(path, opts, mode=0,lngInput=lngInput,aLast=aCleaned)

import requests
def transcribe_with_gladia(audio_path, source_lang, target_lang):
    upload_url = "https://api.gladia.io/v2/upload"
    transcribe_url = "https://api.gladia.io/v2/pre-recorded"
    
    headers = {
        "x-gladia-key": "aac97688-7e14-4274-8e03-cbf2a8212828"
    }

    try:
        # Step 1: Upload the file
        with open(audio_path, "rb") as audio_file:
            print("audio_path: "+ audio_path)
            files = {"audio": (os.path.basename(audio_path), audio_file, "audio/mpeg")}
            upload_response = requests.post(upload_url, files=files, headers=headers)
        
        if upload_response.status_code != 200:
            print(f"Error uploading file: {upload_response.status_code}")
            print(upload_response.text)
            return json.dumps({"text": "", "srt": "", "json": []})

        upload_result = upload_response.json()
        audio_url = upload_result["audio_url"]

        # Step 2: Request transcription
        transcribe_headers = {
            "x-gladia-key": "aac97688-7e14-4274-8e03-cbf2a8212828",
            "Content-Type": "application/json"
        }
        
        payload = {
            "audio_url": audio_url,
            "detect_language": True,
            "language": source_lang,
            "translation": True,
            "translation_config": {
                "target_languages": [target_lang],
                "model": "base",
                "match_original_utterances": True
            },
            "diarization": True,
            "subtitles": True,
            "subtitles_config": {
                "formats": ["srt"],
            },
        }

        transcribe_response = requests.post(transcribe_url, json=payload, headers=transcribe_headers)
        if transcribe_response.status_code == 200 or transcribe_response.status_code == 201:
            transcribe_result = transcribe_response.json()
            result_url = transcribe_result["result_url"]
            
            # Step 3: Fetch the result with polling
            max_attempts = 12  # 1 minute total (5 seconds * 12)
            attempts = 0
            while attempts < max_attempts:
                result_response = requests.get(result_url, headers=headers)
                
                if result_response.status_code == 200 or result_response.status_code == 201:
                    gladia_result = result_response.json()
                    
                    if gladia_result.get("status") == "done":
                        formatted_result = convert_gladia_to_internal_format(gladia_result)
                        return json.dumps(formatted_result)
                    else:
                        print(f"Gladia result not ready. Status: {gladia_result.get('status')}. Waiting 5 seconds...")
                        time.sleep(5)
                        attempts += 1
                else:
                    print(f"Error fetching result from Gladia API: {result_response.status_code}")
                    print(result_response.text)
                    return json.dumps({"text": "", "srt": "", "json": []})

            print("Max attempts reached. Gladia result not ready.")
            return json.dumps({"text": "", "srt": "", "json": []})
        else:
            print(f"Error requesting transcription from Gladia API: {transcribe_response.status_code}")
            print(transcribe_response.text)
            return json.dumps({"text": "", "srt": "", "json": []})
    except requests.exceptions.RequestException as e:
        print(f"Error connecting to Gladia API: {e}")
        return json.dumps({"text": "", "srt": "", "json": []})