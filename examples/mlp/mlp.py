##################################################
#Copyright (c) 2018, Xilinx, Inc.
#All rights reserved.
#
#Redistribution and use in source and binary forms, with or without modification,
#are permitted provided that the following conditions are met:
#
#1. Redistributions of source code must retain the above copyright notice,
#this list of conditions and the following disclaimer.
#
#2. Redistributions in binary form must reproduce the above copyright notice,
#this list of conditions and the following disclaimer in the documentation
#and/or other materials provided with the distribution.
#
#3. Neither the name of the copyright holder nor the names of its contributors
#may be used to endorse or promote products derived from this software
#without specific prior written permission.
#
#THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
#ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
#THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.
#IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
#INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
#PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
#HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
#OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE,
#EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
##################################################

from __future__ import print_function
import numpy as np
import pandas as pd
from keras.utils import np_utils
from sklearn.preprocessing import LabelEncoder
from keras.models import Sequential, Model
from keras.layers import Dense
from keras.callbacks import Callback, ModelCheckpoint
import argparse
import math
from keras_rt import KerasRT
from keras_spmv_rt import KerasSpmvRT
import gemx

#Quantization parameters to bring fp32 ranges to fit into int16; parameters are derived offline ( see quantize.xlsx )
g_wgt_scale = [155275.3311, 121798.1115, 71553.71463]
g_post_scale = [ [5,19], [2,18] , [3,23] ]   
#g_post_scale = [ [43.88706261,4833803.96], [39.4,5345361.346] , [1, 2819547.236] ]    
 
g_in_scale = 31.13053392

def train(train_fd, predictors, train_data, num_classes):
    
    # We will use a multi-layer perceptron classification model for random search.
    # Create model
    #estimator = KerasClassifier(build_fn=create_keras_model(len(predictors), len(train[target].unique())), epochs=200, batch_size=5, verbose=0)
    model = create_keras_model(len(predictors), num_classes )
    # Compile model
    model.compile(loss='categorical_crossentropy', optimizer='adam', metrics=['accuracy'])
        
    modelcheckpoint_callback = ModelCheckpoint("./best_model.h5", monitor='val_loss',mode='min', save_best_only=True, save_weights_only=True)
    
    model.fit(train_fd[predictors], train_data, epochs=200, batch_size=50, callbacks=[modelcheckpoint_callback], validation_split=0.20, shuffle=True)

def predict_hwemu ( weights, test_data, num_classes):
    model = create_keras_model(test_data.values.shape[1], num_classes )
    model.load_weights(weights)
    return compute_standalone_hwemu( test_data.values, model.get_weights())

def predict_cpu ( weights, test_data, num_classes ):
    model = create_keras_model(test_data.values.shape[1], num_classes )
    model.load_weights(weights)
    predictions = model.predict(test_data)
    
    #layer_name = 'd1'
    #intermediate_layer_model = Model(inputs=model.input,
    #                              outputs=model.get_layer(layer_name).output)
    #intermediate_output = intermediate_layer_model.predict(test_data)
    #return intermediate_output

    return predictions

def predict_fpga( args, test_data, num_classes, xclbin_prop):
    model = create_keras_model(test_data.values.shape[1], num_classes )
    model.load_weights(args.model)
    
    fpga_rt = KerasRT(model, xclbin_prop, g_wgt_scale, g_post_scale)
    result = fpga_rt.predict(test_data.values, g_in_scale)

    #run softmax on CPU
    for i in range(result.shape[0]):
        result[i,:] = softmax(result[i,:])
        
    return result
    
def predict_spmv_fpga( args, test_data, num_classes, xclbin_prop):
    model = create_keras_model(test_data.values.shape[1], num_classes )
    model.load_weights(args.model)
    
    fpga_rt = KerasSpmvRT(model, test_data.values.shape[0], g_wgt_scale, xclbin_prop)
    result = fpga_rt.predict(test_data.values, g_in_scale)

    #run softmax on CPU
    for i in range(result.shape[0]):
        result[i,:] = softmax(result[i,:])        
    return result

def softmax(x):
    """Compute softmax values for each sets of scores in x."""
    e_x = np.exp(x - np.max(x))
    return e_x / e_x.sum()

def compute_dense(weight, bias, inp, scalein=1, post_scale=1):
    scaledin = inp*scalein
    inp16 = np.int16(scaledin)#input from previous stage to 16bits
    m64 = np.matmul(np.int64(inp16), np.int64(weight))#intermediate accumulation to 64 bits
    
    output64 = m64
    if bias is not None:
        bias64 = np.int64(bias)#bias to 64 bits
        output64 = m64 + bias64
        
    o64d = output64/(2**post_scale[1])
    #o64d = output64/post_scale[1]
    o64m = o64d*post_scale[0]
    output = np.int16(o64m)#scale down for 16 bits
    return output
    
def compute_standalone_hwemu( inp, wb ):
    weights = wb[0::2]
    bias = wb[1::2]

    #quantization
    w_int16 = [ np.int16(a*b) for a,b in zip(weights, g_wgt_scale)]
    b_int32 = [ np.int32(a*b) for a,b in zip(bias, g_wgt_scale)]
        
    o1 = compute_dense ( w_int16[0], b_int32[0], inp, g_in_scale, g_post_scale[0])
    #print ("o1 range (", np.min(o1), ",", np.max(o1), ")")
    o1[o1 < 0] = 0
    
    o2 = compute_dense ( w_int16[1], b_int32[1], o1, 1, g_post_scale[1])
    #print ("o2 range (", np.min(o2), ",", np.max(o2), ")")    
    o2[o2 < 0] = 0
    o3 = compute_dense ( w_int16[2], b_int32[2], o2, 1, g_post_scale[2])
    #print ("o3 range (", np.min(o3), ",", np.max(o3), ")")
    #softmax
    for i in range(o3.shape[0]):
        o3[i,:] = softmax(np.float64(o3[i,:]))
    return o3

def compute_standalone( inp, wb ):
    print ("inp (", np.min(inp), ",", np.max(inp))
    for i,w in enumerate(wb):
        print ( "w", i, ": ", np.min(w), ", ", np.max(w))
        
    o1 = np.matmul ( inp, wb[0])
    o1 = o1 + wb[1]
    print ("o1 (", np.min(o1), ",", np.max(o1))
    o1[o1 < 0] = 0

    o2 = np.matmul ( o1, wb[2])
    o2 = o2 + wb[3]
    print ("o2 (", np.min(o2), ",", np.max(o2))    
    o2[o2 < 0] = 0
    o3 = np.matmul ( o2, wb[4])
    print ("o3 (", np.min(o3), ",", np.max(o3))    
    o3 = o3 + wb[5]
    #softmax
    for i in range(o3.shape[0]):
        o3[i,:] = softmax(np.float64(o3[i,:]))
    return o3
 
def compare_results ( expected, actual):
    e_r = np.around(expected,decimals=3)
    a_r = np.around(actual, decimals=3)
    if np.array_equal (e_r, a_r):
        print ("SUCCESS!!!")
    else:
        diff = e_r - a_r
        num_diff = 0
        for i in range (e_r.shape[0]):
            if not np.array_equal( e_r[i,:] , a_r[i,:]):
                print("line", i+1, "is different")
                num_diff += 1
                
        print ( num_diff , "/", e_r.shape[0], "incorrect")
        np.savetxt("out.np", a_r, fmt="%f")
        np.savetxt("out_golden.np", e_r, fmt="%f")
        np.savetxt("diff.np", e_r - a_r, fmt="%f")
    
def create_keras_model(in_dims, num_classes):
    '''
    Generate a simple Keras model.
    '''  
    model = Sequential()
    model.add(Dense(100, input_dim=in_dims, activation='relu', name='d1'))
    model.add(Dense(25, activation='relu', name='d2'))
    model.add(Dense(num_classes, activation='softmax', name='d3'))
    #model.add(Dense(num_classes, name='d3'))
    model.summary()
    return model

if  __name__ == '__main__':
    np.random.seed(27)
    parser = argparse.ArgumentParser(description='GEMX')
    parser.add_argument('--data', required = True, help='inference data file')
    parser.add_argument('--model', required = True, help='model')
    #parser.add_argument('--device', default = 'fpga', choices=['cpu', 'fpga'], help='choose cpu or FPGA execution')    
    parser.add_argument('--xclbin', required = True, help='file path to FPGA bitstream')
    parser.add_argument('--cfg', required = True, help='file describing properties of .xclbin')
    parser.add_argument('--gemxlib', required = True, help='file path to GEMX host code shared library')
    parser.add_argument('--engine', default = 'fcn', choices=['fcn','spmv'],help='choose fcn or spmv engine')
    args = parser.parse_args()
    xclbin_prop = gemx.parse_cfg(args.cfg)

    #load xclbin 
    if args.engine == 'fcn':
        gemx.createFCNHandle( args, xclbin_prop )
    else:
        gemx.createSPMVHandle( args, xclbin_prop )
        
    train_fd = pd.read_csv(args.data) # Load training data.
    IDcol = 'Run' # A column used to identified the run for data collection; not an independent variable.
    target = 'Class' # The column name for our dependent variable.
    predictors = [x for x in train_fd.columns if x not in [target, IDcol]] # Define column names to use as independent variables.
    
    # Encode class values as integers
    encoder = LabelEncoder()
    encoder.fit(train_fd[target])
    encoded_Y = encoder.transform(train_fd[target])
    # Convert integers to dummy variables (i.e. one hot encoded)
    train_y = np_utils.to_categorical(encoded_Y)

    #hwemu_out = predict_hwemu( args.model,  train_fd[predictors], len(train_fd[target].unique()) )
    if args.engine == 'fcn':
        fpga_out = predict_fpga( args, train_fd[predictors], len(train_fd[target].unique()), xclbin_prop)
    else:
        fpga_out = predict_spmv_fpga( args, train_fd[predictors], len(train_fd[target].unique()), xclbin_prop)
    cpu_out = predict_cpu( args.model, train_fd[predictors], len(train_fd[target].unique()) )
    #compare_results ( hwemu_out, fpga_out)      
    compare_results ( cpu_out, fpga_out)
    #compare_results ( cpu_out, hwemu_out)    
