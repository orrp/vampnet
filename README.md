# VampNet

This repository contains recipes for training generative music models on top of the Lyrebird Audio Codec.

# Setting up

Requires Python 3.9 or later. 

install [audiotools](https://github.com/descriptinc/audiotools.git) 
```bash
git clone https://github.com/descriptinc/audiotools.git
cd audiotools
pip install -e .
```

install the [`Descript Audio Codec`](https://github.com/descriptinc/descript-audio-codec.git). 
```bash
git clone https://github.com/descriptinc/descript-audio-codec.git
cd descript-audio-codec
pip install -e .
```

now, install VampNet

```bash
git clone https://github.com/hugofloresgarcia/vampnet.git
pip install -e ./vampnet
```

## A note on argbind
This repository relies on [argbind](https://github.com/pseeth/argbind) to manage CLIs and config files. 
Config files are stored in the `conf/` folder. 

## Getting the Pretrained Models

### Licensing for Pretrained Models: 
The weights for the models are licensed [`CC BY-NC-SA 4.0`](https://creativecommons.org/licenses/by-nc-sa/4.0/deed.ml). Likewise, any VampNet models fine-tuned on the pretrained models are also licensed [`CC BY-NC-SA 4.0`](https://creativecommons.org/licenses/by-nc-sa/4.0/deed.ml).

Download the pretrained models from [this link](https://drive.google.com/file/d/1ZIBMJMt8QRE8MYYGjg4lH7v7BLbZneq2/view?usp=sharing). Then, extract the models to the `models/` folder. 


# Usage

First, you'll want to set up your environment
```bash
source ./env/env.sh
```

## Launching the Gradio Interface
You can launch a gradio UI to play with vampnet. 

```bash
python demo.py --args.load conf/interface.yml --Interface.device cuda
```

# Training / Fine-tuning 

## Training a model

To train a model, run the following script: 

```bash
python scripts/exp/train.py --args.load conf/vampnet.yml --save_path /path/to/checkpoints
```

You can edit `conf/vampnet.yml` to change the dataset paths or any training hyperparameters. 

For coarse2fine models, you can use `conf/c2f.yml` as a starting configuration. 

See `python scripts/exp/train.py -h` for a list of options.

## Fine-tuning
To fine-tune a model, use the script in `scripts/exp/fine_tune.py` to generate 3 configuration files: `c2f.yml`, `coarse.yml`, and `interface.yml`. 
The first two are used to fine-tune the coarse and fine models, respectively. The last one is used to launch the gradio interface.

```bash
python scripts/exp/fine_tune.py "/path/to/audio1.mp3 /path/to/audio2/ /path/to/audio3.wav" <fine_tune_name>
```

This will create a folder under `conf/<fine_tune_name>/` with the 3 configuration files.

The save_paths will be set to `runs/<fine_tune_name>/coarse` and `runs/<fine_tune_name>/c2f`. 

launch the coarse job: 
```bash
python scripts/exp/train.py --args.load conf/<fine_tune_name>/coarse.yml 
```

this will save the coarse model to `runs/<fine_tune_name>/coarse/ckpt/best/`.

launch the c2f job: 
```bash
python  scripts/exp/train.py --args.load conf/<fine_tune_name>/c2f.yml 
```

launch the interface: 
```bash
python  demo.py --args.load conf/generated/<fine_tune_name>/interface.yml 
```


