import tensorflow as tf
import numpy as np

class MANNCell():
    def __init__(self, rnn_size, memory_size, memory_vector_dim, head_num, gamma=0.95,
                 reuse=False, k_strategy='separate'):
        self.rnn_size = rnn_size
        self.memory_size = memory_size                                   ### Number of memory locations (N)
        self.memory_vector_dim = memory_vector_dim                       ### The vector size at each location (M)
        self.head_num = head_num                                         # #(read head) == #(write head)  ### this num is 4 by default. The given value 1 in README.md is just a representation.
        self.reuse = reuse
        self.controller = tf.nn.rnn_cell.BasicLSTMCell(self.rnn_size)    ### use LSTM as controller
        self.step = 0
        self.gamma = gamma
        self.k_strategy = k_strategy

    ### output, state = cell(tf.concat([self.x_image[:, t, :], self.x_label[:, t, :]], axis=1), state)
    ### x = tf.concat([self.x_image[:, t, :], self.x_label[:, t, :]], axis=1)
    def __call__(self, x, prev_state):
    ### only compute one time step.
        prev_read_vector_list = prev_state['read_vector_list']      # read vector (the content that is read out, length = memory_vector_dim)
        prev_controller_state = prev_state['controller_state']      # state of controller (LSTM hidden state)

        # x + prev_read_vector -> controller (RNN) -> controller_output

        ### controller_input has batch_size items, each item is (imagew*h+n_classes+prev_read_vector_list)
        ### i.e. controller_input includes the information of image, label and read_vector_list.
        controller_input = tf.concat([x] + prev_read_vector_list, axis=1)
        with tf.variable_scope('controller', reuse=self.reuse):
            ### new_h, new_state = self.controller(inputs, state) 
            ### inputs: [batch_size, input_size]  state: 2*[batch_size, num_units] init by LSTM's member function.
            ### controller_output: [batch_size, ]
            controller_output, controller_state = self.controller(controller_input, prev_controller_state)

        # controller_output     -> k (dim = memory_vector_dim, compared to each vector in M)
        #                       -> a (dim = memory_vector_dim, add vector, only when k_strategy='separate')
        #                       -> alpha (scalar, combination of w_r and w_lu)

        if self.k_strategy == 'summary':
            num_parameters_per_head = self.memory_vector_dim + 1           ### sig_alpha, here we can't determine what '+1' means, look at line62, 65.
        elif self.k_strategy == 'separate':
            num_parameters_per_head = self.memory_vector_dim * 2 + 1
        total_parameter_num = num_parameters_per_head * self.head_num
        with tf.variable_scope("o2p", reuse=(self.step > 0) or self.reuse):
            o2p_w = tf.get_variable('o2p_w', [controller_output.get_shape()[1], total_parameter_num],
                                    initializer=tf.random_uniform_initializer(minval=-0.1, maxval=0.1))
            o2p_b = tf.get_variable('o2p_b', [total_parameter_num],
                                    initializer=tf.random_uniform_initializer(minval=-0.1, maxval=0.1))
            parameters = tf.nn.xw_plus_b(controller_output, o2p_w, o2p_b)   ### [batch_size,total_parameter_num (M+1)*head_num]
        head_parameter_list = tf.split(parameters, self.head_num, axis=1)   ### split parameters into head_parameters [head_num,batch_size,M+1]
        print(len(head_parameter_list))                                     ### split=S along dim(=N), return [S,[...,N/S,...]]

        ### loop1: LSTM itself:
        ### controller_input (image, shifted_label, read_vector_list), controller_state
        ### output_A: controller_output -> (k, alpha) * head_num
        ### output_B: controller_state |-> S1 (LSTM update)
        
        ### loop2: MEMORY conduction / update:
        ### init2: (prev_) M, w_u, w_r + (k, alpha)
        ### prev_w_u -> prev_w_lu 
        ### k, prev_M -> w_r |-> S2
        ### alpha, prev_w_r, prev_w_u -> w_w |-> S3
        ### prev_w_u, w_r, w_w -> w_u |-> S4
        ### prev_M, prev_w_u, w_w, k -> M |-> S5  (writing, update M)
        ### w_r, M -> read_vector |-> S6

        prev_w_r_list = prev_state['w_r_list']      # vector of weightings (blurred address) over locations
        prev_M = prev_state['M']
        prev_w_u = prev_state['w_u']
        prev_indices, prev_w_lu = self.least_used(prev_w_u)
        w_r_list = []
        w_w_list = []
        k_list = []
        a_list = []
        # p_list = []   # For debugging
        for i, head_parameter in enumerate(head_parameter_list):            ### the outmost loop, i.e. iteration along head_num.
            with tf.variable_scope('addressing_head_%d' % i):
                k = tf.tanh(head_parameter[:, 0:self.memory_vector_dim], name='k')
                if self.k_strategy == 'separate':
                    a = tf.tanh(head_parameter[:, self.memory_vector_dim:self.memory_vector_dim * 2], name='a')
                sig_alpha = tf.sigmoid(head_parameter[:, -1:], name='sig_alpha')
                w_r = self.read_head_addressing(k, prev_M)                  ### [B,M]*[B,N,M] -> [B,N] (memory_size)
                w_w = self.write_head_addressing(sig_alpha, prev_w_r_list[i], prev_w_lu)
            w_r_list.append(w_r)                                            ### [head_num, batch_size, memory_size]
            w_w_list.append(w_w)
            k_list.append(k)
            if self.k_strategy == 'separate':
                a_list.append(a)
            # p_list.append({'k': k, 'sig_alpha': sig_alpha, 'a': a})   # For debugging

        w_u = self.gamma * prev_w_u + tf.add_n(w_r_list) + tf.add_n(w_w_list)   # eq (20)

        # Set least used memory location computed from w_(t-1)^u to zero

        M_ = prev_M * tf.expand_dims(1. - tf.one_hot(prev_indices[:, -1], self.memory_size), dim=2)

        # Writing

        M = M_
        with tf.variable_scope('writing'):
            for i in range(self.head_num):                                  ### conduct write ops for 'head_num' times.
                w = tf.expand_dims(w_w_list[i], axis=2)                     ### update memory for 'head_num' times and melt them toegether.
                if self.k_strategy == 'summary':
                    k = tf.expand_dims(k_list[i], axis=1)
                elif self.k_strategy == 'separate':
                    k = tf.expand_dims(a_list[i], axis=1)
                M = M + tf.matmul(w, k)

        # Reading

        read_vector_list = []
        with tf.variable_scope('reading'):
            for i in range(self.head_num):                                  ### conduct write ops for 'head_num' times.
                read_vector = tf.reduce_sum(tf.expand_dims(w_r_list[i], dim=2) * M, axis=1)
                read_vector_list.append(read_vector)                        ### store the read content seperately.

        # controller_output -> NTM output

        NTM_output = tf.concat([controller_output] + read_vector_list, axis=1)

        state = {                                       ### see the definition of initial state to figure out tensors' shape.
            'controller_state': controller_state,       ### self.controller.zero_state(batch_size, dtype), init by LSTM's init funciton.
            'read_vector_list': read_vector_list,       ### [head_num=4, batch_size, memory_vector_dim]
            'w_r_list': w_r_list,                       ### [head_num=4, batch_size, memory_size]
            'w_w_list': w_w_list,
            'w_u': w_u,                                 ### [batch_size, memory_size]
            'M': M,                                     ### [batch_size, memory_size, memory_vector_dim]
        }

        self.step += 1
        return NTM_output, state

    def read_head_addressing(self, k, prev_M):
        with tf.variable_scope('read_head_addressing'):

            # Cosine Similarity

            k = tf.expand_dims(k, axis=2)
            inner_product = tf.matmul(prev_M, k)
            k_norm = tf.sqrt(tf.reduce_sum(tf.square(k), axis=1, keep_dims=True))
            M_norm = tf.sqrt(tf.reduce_sum(tf.square(prev_M), axis=2, keep_dims=True))
            norm_product = M_norm * k_norm
            K = tf.squeeze(inner_product / (norm_product + 1e-8))                   # eq (17)

            # Calculating w^c

            K_exp = tf.exp(K)
            w = K_exp / tf.reduce_sum(K_exp, axis=1, keep_dims=True)                # eq (18)

            return w

    def write_head_addressing(self, sig_alpha, prev_w_r, prev_w_lu):
        with tf.variable_scope('write_head_addressing'):

            # Write to (1) the place that was read in t-1 (2) the place that was least used in t-1

            return sig_alpha * prev_w_r + (1. - sig_alpha) * prev_w_lu              # eq (22)

    def least_used(self, w_u):
        _, indices = tf.nn.top_k(w_u, k=self.memory_size)
        w_lu = tf.reduce_sum(tf.one_hot(indices[:, -self.head_num:], depth=self.memory_size), axis=1)
        return indices, w_lu

    def zero_state(self, batch_size, dtype):
        one_hot_weight_vector = np.zeros([batch_size, self.memory_size])
        one_hot_weight_vector[..., 0] = 1                                            ### [[1,0,0,...],[1,0,0,...],...]
        one_hot_weight_vector = tf.constant(one_hot_weight_vector, dtype=tf.float32)
        with tf.variable_scope('init', reuse=self.reuse):
            state = {
                'controller_state': self.controller.zero_state(batch_size, dtype),
                'read_vector_list': [tf.zeros([batch_size, self.memory_vector_dim])
                                     for _ in range(self.head_num)],
                'w_r_list': [one_hot_weight_vector for _ in range(self.head_num)],
                'w_u': one_hot_weight_vector,
                'M': tf.constant(np.ones([batch_size, self.memory_size, self.memory_vector_dim]) * 1e-6, dtype=tf.float32)
            }
            return state
