############################################################################
## Copyright 2021 Hewlett Packard Enterprise Development LP
## Licensed under the Apache License, Version 2.0 (the "License"); you may
## not use this file except in compliance with the License. You may obtain
## a copy of the License at
##
##    http://www.apache.org/licenses/LICENSE-2.0
##
## Unless required by applicable law or agreed to in writing, software
## distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
## WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
## License for the specific language governing permissions and limitations
## under the License.
############################################################################

import datetime
import numpy as np
import os
from swarm import SwarmCallback
import time
import torch 
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from data_loader import myDataSet
from parameters import train_conf
from utils import myError, lrDecline, optimizerChoose, lossCaculate
from torch.utils.data import DataLoader

import pdb
default_max_epochs = 1000
default_min_peers = 3
# maxEpochs = 2
trainPrint = True
# tell swarm after how many batches
# should it Sync. We are not doing 
# adaptiveRV here, its a simple and quick demo run
swSyncInterval = 128 
import csv
class VPTLSTM(nn.Module):

    def __init__(self, rnn_size, embedding_size, input_size, output_size, grids_width, grids_height, dropout_par,
                 device):

        super(VPTLSTM, self).__init__()
        ######参数初始化##########
        self.device = device
        self.rnn_size = rnn_size  # hidden size默认128
        self.embedding_size = embedding_size  # 空间坐标嵌入尺寸64，每个状态用64维向量表示
        self.input_size = input_size  # 输入尺寸6,特征向量长度
        self.output_size = output_size  # 输出尺寸5
        self.grids_width = grids_width
        self.grids_height = grids_height
        self.dropout_par = dropout_par

        ############网络层初始化###############
        # 输入embeded_input,hidden_states
        self.cell = nn.LSTMCell(2 * self.embedding_size, self.rnn_size)

        # 输入Embed层，将长度为input_size的vec映射到embedding_size
        self.input_embedding_layer = nn.Linear(self.input_size, self.embedding_size)

        # 输入[vehicle_num,grids_height,grids_width,rnn_size]  [26,39,5,128]
        # 输出[vehicle_num,grids_height-12,grids_width-4,rnn_size*4]  [26,27,1,32]
        self.social_tensor_conv1 = nn.Conv2d(in_channels=self.rnn_size, out_channels=self.rnn_size // 2, kernel_size=(5,3),
                                             stride=(2,1))
        self.social_tensor_conv2 = nn.Conv2d(in_channels=self.rnn_size // 2, out_channels=self.rnn_size // 4,
                                             kernel_size=(5,3), stride=1)
        self.social_tensor_embed = nn.Linear((self.grids_height - 15) * (self.grids_width - 4) * self.rnn_size // 4,
                                             self.embedding_size)

        # 输出Embed层，将长度为64的hidden_state映射到5
        self.output_layer = nn.Linear(self.rnn_size, self.output_size)

        self.relu = nn.ReLU()
        self.dropout = nn.Dropout(self.dropout_par)

    def forward(self, x_seq, grids, hidden_states, cell_states, long_term=False):
        '''
        模型前向传播
        params:
        x_seq: 输入的一组数据tensor(seq_len=99,vehicle_num=26,input_size=9)
        grids: 相关性判断矩阵tensor(99,26,39,5)
        hidden_states: 隐藏状态，tensor(vehicle_num=26,rnn_size=128)
        cell_states: 记忆胞元，tensor(vehicle_num=26,rnn_size=128)
        long_term:长时预测模式

        return:
        long_term=0:
        对应99个二维高斯函数[seq_length=99,vehicle_num=26,output_size=5]
        long_term ！=0:
        未来5秒预测
        '''
        self.x_seq = x_seq  # [seq_len=99,vehicle_num=26,input_size=9]
        self.grids = grids  # [seq_len=99,vehicle_num=26,grids_height=39,grid_width=5]
        self.hidden_states = hidden_states  # [vehicle_num=26,rnn_size=128]
        self.cell_states = cell_states  # [vehicle_num=26,rnn_size=128]

        if not long_term:
            outputs = []
            for frame_index, frame in enumerate(self.x_seq):
                output = self.frameForward(frame, grid=self.grids[frame_index])

                outputs.append(output)
            return torch.stack(outputs, dim=0)
        else:
            outputs = []
            last_point = None
            for frame_index, frame in enumerate(self.x_seq):
                last_point = self.frameForward(frame, grid=self.grids[frame_index])
            stable_value = self.x_seq[0, :, 2:7]
            for _ in range(self.x_seq.shape[0]):
                outputs.append(last_point.clone()) ##last_point是在外边的！tensor可以在循环外被保存
                last_point, grid = self.dataMakeUp(stable_value=stable_value, last_point=last_point)
                last_point = self.frameForward(last_point, grid=grid)

            return torch.stack(outputs)

    def frameForward(self, frame, grid):
        '''
        一帧正向传播，更新hidden_state和cell_state,返回下一个点预测结果
        输入：frame：tensor(vehicle_num=26,vec=9)
        输出：output：tensor(vehicle_num=26,vec=5)
        vec=[mx,my,sx,sy,corr]
        '''
        # 得到social_tensor:  tensor(vehicle_num=26,girds_height=39,grids_width=5,rnn_size=128)
        social_tensor = self.getSocialTensor(grid)

        # Embed inputs
        # 输入(vehicle_num,input_size),输出(vehicle_num,embedding_size=64)
        input_embedded = self.dropout(self.relu(self.input_embedding_layer(frame)))

        # Social_tensor的运算
        # 输入tensor(vehicle_num=26,rnn_size=128,girds_height=39,grids_width=5)，
        # 输出tensor(vehicle_num=26,rnn_size=128/2,girds_height=39-6,grids_width=5-2)
        social_tensor = social_tensor.permute(0, 3, 1, 2)
        tensor_embedded = self.dropout(self.relu(self.social_tensor_conv1(social_tensor)))

        # 输入tensor(vehicle_num=26,rnn_size=128/2,girds_height=39-6,grids_width=5-2)
        # 输出tensor(vehicle_num=26,rnn_size=128/4,girds_height=39-12,grids_width=5-4)
        tensor_embedded = self.dropout(self.relu(self.social_tensor_conv2(tensor_embedded)))

        # 输入tensor(vehicle_num=26,rnn_size=128/4,girds_height=39-12,grids_width=5-4)
        # 打平到tensor(vehicle_num=26,-1)
        # 全连接得到tensor(26,embeding_size)
        tensor_embedded = self.dropout(self.relu(self.social_tensor_embed(torch.flatten(tensor_embedded, 1))))

        # 拼接embed后的input和social_tensor  #输入2个(vehicle_num,embedding_size=64)输出(vehicle_num,2*embedding_size)
        concat_embedded = torch.cat((input_embedded, tensor_embedded), 1)

        # LSTM运行一次
        # 输入(vehicle_num,2*embedding_size),(2，[vehicle_num,rnn_size]),输出[2，[vehicle_num,rnn_size]]
        self.hidden_states, self.cell_states = self.cell(concat_embedded, (self.hidden_states, self.cell_states))

        # 计算下一帧output
        # 输入[vehicle_num,rnn_size],输出[vehicle_num,output_size]
        # output=self.sigmoid(self.output_layer(self.hidden_states))
        output = self.output_layer(self.hidden_states)

        return output

    def getSocialTensor(self, one_frame_grids):
        '''

        :param one_frame_grids: 一帧的相关性判断矩阵tensor(vehicle_num=26,grids_height=39,grids_width=5)，没车默认-1
        :return: social_tensor:嵌入相应隐藏张量的状态量tensor(vehicle_num=26,girds_height=39,grids_width=5，rnn_size=128)
        '''
        # 得到一个全为0的空的tensor(vehicle_num=26,grids_height=39,grids_width=5,rnn_size=128)
        social_tensor = torch.zeros_like(one_frame_grids.unsqueeze(-1).expand(-1, -1, -1, self.rnn_size))

        grid_have_vehicle = torch.where(one_frame_grids != -1)  # 收到3个等长tensor，3对应三个维度的索引值
        total_grids = grid_have_vehicle[0].shape[0]  # 总共有多少个有车的grid
        for one_grid in range(total_grids):
            # 三个维度
            target_vehicle_index, grids_height_index, grids_width_index = grid_have_vehicle[0][one_grid], \
                                                                          grid_have_vehicle[1][one_grid], \
                                                                          grid_have_vehicle[2][one_grid]
            social_tensor[target_vehicle_index][grids_height_index][grids_width_index] = self.hidden_states[
                target_vehicle_index]

        return social_tensor

    def getFunction(self, getGrid, road_info, min_Local_Y, max_Local_Y):
        self.getGrid = getGrid
        self.road_info = road_info
        self.min_Local_Y = min_Local_Y
        self.max_Local_Y = max_Local_Y

    def dataMakeUp(self, stable_value, last_point):
        '''
        9个特征[local_x, local_y, v_length, v_width, motor, auto, truck, turn_left, turn_right]
        :param stable_value: tensor(vehicle,5)固有属性v_length, v_width, motor, auto, truck
        :param last_point:上一个循环的tensor(vehicle_num,5)
        :return:
            combine_data: tensor[vehicle_num,vec=9]  补齐的数据
            grid:tensor(vehicle,grid_height,grid_width)
        '''
        combine_data = torch.cat([last_point[:, 0:2], stable_value], dim=1)
        turn_left = torch.as_tensor(combine_data[:, 0] * self.road_info["max_Local_X"] > self.road_info["lane_one_max"],
                                    dtype=torch.int, device=self.device)
        turn_right = torch.as_tensor(combine_data[:, 0] * self.road_info["max_Local_X"] < self.road_info["lane_five_min"], dtype=torch.int, device=self.device)
        combine_data = torch.cat([combine_data, torch.unsqueeze(turn_left, dim=-1).float(), torch.unsqueeze(turn_right, dim=-1).float()], dim=1)

        last_point[:, 0] = last_point[:, 0] * self.road_info["max_Local_X"]
        last_point[:, 1] = last_point[:, 1] * (self.max_Local_Y - self.min_Local_Y) + self.min_Local_Y
        grid = self.getGrid(last_point, from_df=0)
        grid = torch.tensor(np.array(grid), device=self.device, dtype=torch.float32)
        return combine_data, grid
        
def loadData(dataDir):
    # load data from npz format to numpy 
    # path = os.path.join(dataDir,'mnist.npz')
    # with np.load(path) as f:
    #     xTrain, yTrain = f['x_train'], f['y_train']
    #     xTest, yTest = f['x_test'], f['y_test']
    #     xTrain, xTest = xTrain / 255.0, xTest / 255.0
    #
    # # transform numpy to torch.Tensor
    # xTrain, yTrain, xTest, yTest = map(torch.tensor, (xTrain.astype(np.float32),
    #                                                   yTrain.astype(np.int_),
    #                                                   xTest.astype(np.float32),
    #                                                   yTest.astype(np.int_)))
    # # convert torch.Tensor to a dataset
    # yTrain = yTrain.type(torch.LongTensor)
    # yTest = yTest.type(torch.LongTensor)
    # trainDs = torch.utils.data.TensorDataset(xTrain,yTrain)
    # testDs = torch.utils.data.TensorDataset(xTest,yTest)
    conf = train_conf()
    print("*" * 40)
    print("载入数据中")
    train_data = myDataSet(csv_source=conf.train_csv_source, need_col=conf.need_col,
                                output_col=conf.output_col,
                                grids_width=conf.grids_width, grids_height=conf.grids_height,
                                meter_per_grid=conf.meter_per_grid, road=conf.road_name, long_term=conf.long_term)

    test_data = myDataSet(csv_source=conf.test_csv_source, need_col=conf.need_col, output_col=conf.output_col,
                               grids_width=conf.grids_width, grids_height=conf.grids_height,
                               meter_per_grid=conf.meter_per_grid, road=conf.road_name, long_term=conf.long_term)
    train_data_length, test_data_length = train_data.__len__(), test_data.__len__()
    print("数据载入完成")
    return train_data, test_data

def batchExec(x, y, grids, conf,device):
        # 迁移数据至GPU
        x = torch.as_tensor(torch.squeeze(x), dtype=torch.float32, device=device)
        y = torch.as_tensor(torch.squeeze(y), dtype=torch.float32, device=device)
        grids = torch.as_tensor(torch.squeeze(grids), dtype=torch.float32, device=device)

        # hidden_state初始化
        vehicle_num = x.shape[1]
        hidden_states = torch.zeros(vehicle_num, conf.rnn_size, device=device)
        cell_states = torch.zeros(vehicle_num, conf.rnn_size, device=device)
        return x, y, grids, hidden_states, cell_states

def doTrainBatch(model,device,trainLoader,optimizer,epoch,swarmCallback,conf,trainDs):
    model.train()
    for batchIdx, (data, target, train_grids) in enumerate(trainLoader):
        # 处理一组数据
        if batchIdx>10:
            break
        train_x, train_y, train_grids, hidden_states, cell_states =  batchExec(x=data,
                                                                                   y=target,
                                                                                   grids=train_grids,
                                                                                   conf=conf,
                                                                                   device=device)

    # for batchIdx, (data, target) in enumerate(trainLoader):
        if conf.long_term:
            model.getFunction(getGrid=trainDs.getGrid, road_info=trainDs.road_info,
                                 min_Local_Y=trainDs.min_Local_Y, max_Local_Y=trainDs.max_Local_Y)

        # data, target = data.to(device), target.to(device)
        output = model(x_seq=train_x, grids=train_grids, hidden_states=hidden_states, cell_states=cell_states,
                       long_term=conf.long_term)
        # output = model(data)
        # loss = F.nll_loss(output, target)
        loss = lossCaculate(pred=output, true=train_y, conf=conf)
        loss.backward()
        # train_loss_baches.append(loss.item())
        optimizer.step()
        optimizer.zero_grad()
        model.zero_grad()
        if trainPrint and batchIdx % 100 == 0:
            print('Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}'.format(
                  epoch, batchIdx * len(data), len(trainLoader.dataset),
                  100. * batchIdx / len(trainLoader), loss.item()))
        # Swarm Learning Interface
        if swarmCallback is not None:
            swarmCallback.on_batch_end()        

def test(model, device, testLoader,test_data,conf):
    model.eval()
    testLoss = 0
    correct = 0
    all_distance=[]
    acc_count=0
    with torch.no_grad():
        for test_x, test_y, test_grids in (testLoader):
            test_x, test_y, test_grids, hidden_states, cell_states = batchExec(x=test_x, y=test_y,
                                                                                    grids=test_grids, conf=conf,device=device)
            if conf.long_term:
                model.getFunction(getGrid=test_data.getGrid, road_info=test_data.road_info,
                                     min_Local_Y=test_data.min_Local_Y, max_Local_Y=test_data.max_Local_Y)
            out = model(x_seq=test_x, grids=test_grids, hidden_states=hidden_states, cell_states=cell_states,
                           long_term=conf.long_term)
            loss = lossCaculate(pred=out, true=test_y, conf=conf)
            # test_loss_batches.append(loss.item())
            # testLoss+=loss
            #
            # loss = lossCaculate(pred=outputs, true=labels, conf=conf)
            loss += loss.item()
            pred_x, pred_y = out[9, 0, 0] * 24, out[9, 0, 1] * (
                        test_data.max_Local_Y - test_data.min_Local_Y) + test_data.min_Local_Y
            true_x, true_y = test_x[9, 0, 0] * 24, test_x[9, 0, 1] * (
                        test_data.max_Local_Y - test_data.min_Local_Y) + test_data.min_Local_Y
            distance = ((true_y - pred_y) ** 2 + (true_x - pred_x) ** 2) ** 0.5
            if distance < 10:
                acc_count += 1
            all_distance.append(distance)

        # for data, target in testLoader:
        #     data, target = data.to(device), target.to(device)
        #     output = model(data)
        #     testLoss += F.nll_loss(output, target, reduction='sum').item()  # sum up batch loss
        #     pred = output.argmax(dim=1, keepdim=True)  # get the index of the max log-probability
        correct =0
    accuracy=acc_count/len(all_distance)
    f = open('test_acc.csv', 'a', encoding='utf-8')
    csv_writer = csv.writer(f)
    csv_writer.writerow([(sum(all_distance)/len(all_distance)), str(loss),str(accuracy)])
    f.close()

def test_butnot_save(model, device, testLoader, test_data, conf):
    model.eval()
    testLoss = 0
    correct = 0
    all_distance = []
    acc_count = 0
    with torch.no_grad():
        for test_x, test_y, test_grids in (testLoader):
            test_x, test_y, test_grids, hidden_states, cell_states = batchExec(x=test_x, y=test_y,
                                                                                   grids=test_grids, conf=conf,
                                                                                   device=device)
            if conf.long_term:
                model.getFunction(getGrid=test_data.getGrid, road_info=test_data.road_info,
                                      min_Local_Y=test_data.min_Local_Y, max_Local_Y=test_data.max_Local_Y)
            out = model(x_seq=test_x, grids=test_grids, hidden_states=hidden_states, cell_states=cell_states,
                            long_term=conf.long_term)
            loss = lossCaculate(pred=out, true=test_y, conf=conf)
            # test_loss_batches.append(loss.item())
            # testLoss+=loss
            #
            # loss = lossCaculate(pred=outputs, true=labels, conf=conf)
            loss += loss.item()
            pred_x, pred_y = out[9, 0, 0] * 24, out[9, 0, 1] * (
                    test_data.max_Local_Y - test_data.min_Local_Y) + test_data.min_Local_Y
            true_x, true_y = test_x[9, 0, 0] * 24, test_x[9, 0, 1] * (
                    test_data.max_Local_Y - test_data.min_Local_Y) + test_data.min_Local_Y
            distance = ((true_y - pred_y) ** 2 + (true_x - pred_x) ** 2) ** 0.5
            if distance < 10:
                acc_count += 1
            all_distance.append(distance)

            # for data, target in testLoader:
            #     data, target = data.to(device), target.to(device)
            #     output = model(data)
            #     testLoss += F.nll_loss(output, target, reduction='sum').item()  # sum up batch loss
            #     pred = output.argmax(dim=1, keepdim=True)  # get the index of the max log-probability
        correct = 0
        # accuracy = acc_count / len(all_distance)
        # f = open('test_acc.csv', 'a', encoding='utf-8')
        # csv_writer = csv.writer(f)
        # csv_writer.writerow([(sum(all_distance) / len(all_distance)), str(loss), str(accuracy)])
        # f.close()
    return loss.item()


def check_load_model():
    flag=False
    with open("model_Flag.txt", "r") as f:
        data = f.readline()
        print("读取的值",data)
        if data=="True":
            flag=True
    return flag

def check_global_model():
    flag=False
    with open("Globalmodel_Flag.txt", "r") as f:
        data = f.readline()
        print("读取的值",data)
        if data=="True":
            flag=True
    return flag

def main():
    conf = train_conf()
    dataDir = os.getenv('DATA_DIR', './data')
    modelDir = os.getenv('MODEL_DIR', './model')
    max_epochs = int(os.getenv('MAX_EPOCHS', str(default_max_epochs)))
    min_peers = int(os.getenv('MIN_PEERS', str(default_min_peers)))
    batchSz = 128 # this gives 97% accuracy on CPU
    trainDs, testDs = loadData(dataDir)
    useCuda = torch.cuda.is_available()
    device = torch.device("cuda" if useCuda else "cpu")  
    model = VPTLSTM(rnn_size=conf.rnn_size, embedding_size=conf.embedding_size, input_size=conf.input_size,
                          output_size=conf.output_size,
                          grids_width=conf.grids_width, grids_height=conf.grids_height, dropout_par=conf.dropout_par,
                          device=device).to(device)
    model_tmp=VPTLSTM(rnn_size=conf.rnn_size, embedding_size=conf.embedding_size, input_size=conf.input_size,
                          output_size=conf.output_size,
                          grids_width=conf.grids_width, grids_height=conf.grids_height, dropout_par=conf.dropout_par,
                          device=device).to(device)
    model_name = 'mnist_pyt'
    opt = optim.Adam(model.parameters())
    trainLoader = torch.utils.data.DataLoader(trainDs,batch_size=1, shuffle=True)
    testLoader = torch.utils.data.DataLoader(testDs,batch_size=1, shuffle=True)
    
    # Create Swarm callback
    swarmCallback = None
    swarmCallback = SwarmCallback(sync_interval=swSyncInterval,
                                  min_peers=min_peers,
                                  val_data=testDs,
                                  val_batch_size=batchSz,
                                  model_name=model_name,
                                  model=model)
    # initalize swarmCallback and do first sync 
    swarmCallback.on_train_begin()
    save_flag=0
        
    for epoch in range(1, max_epochs + 1):
        model_read_flag=check_load_model()
        global_modelFlag=check_global_model()
        if save_flag!=0:
            if global_modelFlag==False:
                with open("Globalmodel_Flag.txt", "w") as f:
                    f.write("True")
                # model.load_state_dict(torch.load('net_2.pkl'))
                model_tmp.load_state_dict(torch.load('net_2.pkl'),map_location=device)
                loss1_=test_butnot_save(model_tmp,device,testLoader,testDs,conf)
                loss2_ = test_butnot_save(model, device, testLoader, testDs, conf)
                if loss1_<loss2_:
                    print("Global model have better performance")
                    model.load_state_dict(torch.load('net_2.pkl'),map_location=device)
                else:
                    print("Local model better, do not load")
            else:
                pass
        else:
            save_flag+=1
            print("Fist epoch do not load")
        #     model_tmp.load_state_dict(torch.load('net.pkl'))
        #     print("需要load模型")
        #     loss1_=test_butnot_save(model_tmp,device,testLoader,testDs,conf)
        #     loss2_ = test_butnot_save(model, device, testLoader, testDs, conf)
        #     if loss1_<loss2_:
        #         print("load成功,新模型效果更好")
        #         model.load_state_dict(torch.load('net.pkl'))
        #     else:
        #         print("load失败,本地模型效果更好")

        doTrainBatch(model,device,trainLoader,opt,epoch,swarmCallback,conf,trainDs)
        test(model,device,testLoader,testDs,conf)
        swarmCallback.on_epoch_end(epoch)
        # Save model and weights
        # save_model_as+pkl
        dirs_1='net_1.pkl'
        torch.save(model.state_dict(), dirs_1)
        dirs_2 = 'net_2.pkl'
        torch.save(model.state_dict(), dirs_2)
        print("epoch={}, 模型已刷新存储至{}".format(epoch, dirs_1))
        # if model_read_flag==True:
        with open("model_Flag.txt", "w") as f:
            f.write("True")
        # model_path = os.path.join(modelDir, model_name, 'saved_model.pt')
        # # Pytorch model save function expects the directory to be created before hand.
        # os.makedirs(os.path.join(modelDir, model_name), exist_ok=True)
        # torch.save(model, model_path)

    # handles what to do when training ends        
    swarmCallback.on_train_end()


    print('Saved the trained model!')
  
if __name__ == '__main__':
  main()
