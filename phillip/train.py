import os, sys
import time
from phillip import RL, util, ssbm
from phillip.default import *
import numpy as np
from collections import defaultdict
import nnpy
import resource
import gc
import tensorflow as tf
#from memory_profiler import profile
import netifaces

# some helpers for debugging memory leaks

def count_objects():
  counts = defaultdict(int)
  for obj in gc.get_objects():
    counts[type(obj)] += 1
  return counts

def diff_objects(after, before):
  diff = {k: after[k] - before[k] for k in after}
  return {k: i for k, i in diff.items() if i}

class Trainer(Default):
  _options = [
    #Option("debug", action="store_true", help="set debug breakpoint"),
    #Option("-q", "--quiet", action="store_true", help="don't print status messages to stdout"),
    Option("init", action="store_true", help="initialize variables"),

    Option("sweeps", type=int, default=1, help="number of sweeps between saves"),
    Option("sweep_limit", type=int, default=-1),
    Option("batches", type=int, default=1, help="number of batches per sweep"),
    Option("batch_size", type=int, default=1, help="number of trajectories per batch"),
    Option("batch_steps", type=int, default=1, help="number of gradient steps to take on each batch"),
    Option("min_collect", type=int, default=1, help="minimum number of experiences to collect between sweeps"),
    Option("max_age", type=int, help="how old an experience can be before we discard it"),
    Option("max_kl", type=float, help="how off-policy an experience can be before we discard it"),
    
    Option("log_interval", type=int, default=10),
    Option("dump", type=str, default="lo", help="interface to listen on for experience dumps"),
    Option('send', type=int, default=1, help="send the network parameters on an nnpy PUB socket"),
    Option("save_interval", type=float, default=10, help="length of time between saves to disk, in minutes"),

    Option("load", type=str, help="path to a json file from which to load params"),
    Option("pop_size", type=int, default=0),

    Option('objgraph', type=int, default=0, help='use objgraph to track memory usage'),
  ]
  
  _members = [
    ("model", RL.RL),
  ]
  
  def __init__(self, load=None, **kwargs):
    if load is None:
      args = {}
    else:
      args = util.load_params(load, 'train')
    
    util.update(args, mode=RL.Mode.TRAIN, **kwargs)
    util.pp.pprint(args)
    Default.__init__(self, **args)

    addresses = netifaces.ifaddresses(self.dump)
    address = addresses[netifaces.AF_INET][0]['addr']

    util.makedirs(self.model.path)
    with open(os.path.join(self.model.path, 'ip'), 'w') as f:
      f.write(address)

    self.experience_socket = nnpy.Socket(nnpy.AF_SP, nnpy.PULL)
    experience_addr = "tcp://%s:%d" % (address, util.port(self.model.path + "/experience"))
    self.experience_socket.bind(experience_addr)

    if self.send:
      self.params_socket = nnpy.Socket(nnpy.AF_SP, nnpy.PUB)
      params_addr = "tcp://%s:%d" % (address, util.port(self.model.path + "/params"))
      print("Binding params socket to", params_addr)
      self.params_socket.bind(params_addr)

    self.sweep_size = self.batches * self.batch_size
    print("Sweep size", self.sweep_size)
    
    if self.init:
      self.model.init()
      self.model.save()
    else:
      self.model.restore()
    
    self.last_save = time.time()
  
  def save(self):
    current_time = time.time()
    
    if current_time - self.last_save > 60 * self.save_interval:
      try:
        self.model.save()
        self.last_save = current_time
      except tf.errors.InternalError as e:
        print(e, file=sys.stderr)

  def train(self):
    before = count_objects()

    sweeps = 0
    step = 0
    global_step = self.model.get_global_step()
    
    times = ['min_collect', 'extra_collect', 'train', 'save']
    averages = {name: util.MovingAverage(.9) for name in times}
    
    timer = util.Timer()
    def split(name):
      averages[name].append(timer.split())
    
    experiences = []
    
    while sweeps != self.sweep_limit:
      timer.reset()
      
      #print('Start: %s' % resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)

      old_len = len(experiences)
      if self.max_age is not None:
        # print("global_step", global_step)
        age_limit = global_step - self.max_age
        is_valid = lambda exp: exp['global_step'] >= age_limit
        experiences = list(filter(is_valid, experiences))
      else:
        is_valid = lambda _: True
      dropped = old_len - len(experiences)
      
      def pull_experience(block=True):
        exp = self.experience_socket.recv(flags=0 if block else nnpy.DONTWAIT)
        return pickle.loads(exp)

      # print("Collecting experiences", len(experiences))
      collected = 0
      doa = 0 # dead on arrival
      while len(experiences) < self.sweep_size or collected < self.min_collect:
        #print("Waiting for experience")
        exp = pull_experience()
        if is_valid(exp):
          experiences.append(exp)
          collected += 1
        else:
          doa += 1
          pass

      split('min_collect')

      # pull in all the extra experiences
      for _ in range(self.sweep_size):
        try:
          exp = pull_experience(False)
          if is_valid(exp):
            experiences.append(exp)
            collected += 1
          else:
            doa += 1
        except nnpy.NNError as e:
          if e.error_no == nnpy.EAGAIN:
            # nothing to receive
            break
          # a real error
          raise e

      ages = np.array([global_step - exp['global_step'] for exp in experiences])
      print("Mean age:", ages.mean())
            
      #print('After collect: %s' % resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
      split('extra_collect')
      
      for _ in range(self.sweeps):
        from random import shuffle
        shuffle(experiences)

        batches = len(experiences) // self.batch_size
        batch_size = (len(experiences) + batches - 1) // batches
        
        use_kls = self.max_kl is not None
        if use_kls:
          kls = []
        
        for batch in util.chunk(experiences, batch_size):
          train_out = self.model.train(batch, self.batch_steps,
                                       log=(step%self.log_interval==0),
                                       kls=use_kls)[-1]
          global_step = train_out['global_step']
          
          if use_kls:
            kls.extend(train_out['kls'])
          
          step += 1
        
        if use_kls:
          print("Mean KL", np.mean(kls))
          old_len = len(experiences)
          experiences = [exp for kl, exp in zip(kls, experiences) if kl <= self.max_kl]
          dropped += old_len - len(experiences)
      
      #print('After train: %s' % resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
      split('train')

      if self.send:
        #self.params_socket.send_string("", zmq.SNDMORE)
        params = self.model.blob()
        blob = pickle.dumps(params)
        #print('After blob: %s' % resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        self.params_socket.send(blob)
        #print('After send: %s' % resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)

      self.save()
      
      #print('After save: %s' % resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
      split('save')
      
      sweeps += 1
      
      if False:
        after = count_objects()
        print(diff_objects(after, before))
        before = after
      
      time_avgs = [averages[name].avg for name in times]
      total_time = sum(time_avgs)
      time_avgs = ['%.3f' % (t / total_time) for t in time_avgs]
      print(sweeps, len(experiences), collected, dropped, doa, total_time, *time_avgs)
      print('Memory usage: %s (kb)' % resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)

      if self.objgraph:
        import objgraph
        #gc.collect()  # don't care about stuff that would be garbage collected properly
        objgraph.show_growth()
  
  def fake_train(self):
    experience = (ssbm.SimpleStateAction * self.model.config.experience_length)()
    experience = ssbm.prepareStateActions(experience)
    experience['initial'] = util.deepMap(np.zeros, self.model.core.hidden_size)
    
    experiences = [experience] * self.batch_size
    
    # For more advanced usage, user can control the tracing steps and
    # dumping steps. User can also run online profiling during training.
    #
    # Create options to profile time/memory as well as parameters.
    builder = tf.profiler.ProfileOptionBuilder
    opts = builder(builder.time_and_memory()).order_by('micros').build()
    opts2 = tf.profiler.ProfileOptionBuilder.trainable_variables_parameter()

    # Collect traces of steps 10~20, dump the whole profile (with traces of
    # step 10~20) at step 20. The dumped profile can be used for further profiling
    # with command line interface or Web UI.
    with tf.contrib.tfprof.ProfileContext('/tmp/train_dir',
                                          trace_steps=range(10, 20),
                                          dump_steps=[20]) as pctx:
      # Run online profiling with 'op' view and 'opts' options at step 15, 18, 20.
      pctx.add_auto_profiling('op', opts, [15, 18, 20])
      # Run online profiling with 'scope' view and 'opts2' options at step 20.
      pctx.add_auto_profiling('scope', opts2, [20])
      # High level API, such as slim, Estimator, etc.
      count = 0
      while count != self.sweep_limit:
        self.model.train(experiences, self.batch_steps)
        count += 1

if __name__ == '__main__':
  from argparse import ArgumentParser
  parser = ArgumentParser()

  for opt in Trainer.full_opts():
    opt.update_parser(parser)

  for policy in RL.policies.values():
    for opt in policy.full_opts():
      opt.update_parser(parser)
      
  parser.add_argument('--fake', action='store_true', help='Train on fake experiences for debugging.')

  args = parser.parse_args()
  trainer = Trainer(**args.__dict__)
  if args.fake:
    trainer.fake_train()
  else:
    trainer.train()

