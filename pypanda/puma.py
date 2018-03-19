from __future__ import print_function

import math
import time
import pandas as pd
import numpy as np
from scipy.stats import zscore
from .timer import Timer

class Puma(object):
    """ Using PUMA to infer gene regulatory network.

    1. Reading in input data (expression data, motif prior, TF PPI data, miR)
    2. Computing coexpression network
    3. Normalizing networks
    4. Running PUMA algorithm
    5. Writing out PUMA network

    Authors: cychen, davidvi, alessandromarin
    """
    def __init__(self, expression_file, motif_file, ppi_file, mir_file, save_memory = False, save_tmp=True):
        # =====================================================================
        # Data loading
        # =====================================================================
        with Timer('Loading expression data ...'):
            self.expression_data = pd.read_table(expression_file, sep='\t', header=None, index_col=0)
            self.gene_names = self.expression_data.index.tolist()
            self.num_genes = len(self.gene_names)
            print('Expression matrix:', self.expression_data.shape)

        with Timer('Loading motif data ...'):
            self.motif_data = pd.read_table(motif_file, sep='\t', header=None)
            self.unique_tfs = sorted(set(self.motif_data[0]))
            self.num_tfs = len(self.unique_tfs)
            print('Unique TFs:', self.num_tfs)

        with Timer('Loading PPI data ...'):
            self.ppi_data = pd.read_table(ppi_file, sep='\t', header=None)
            print('Number of PPIs:', self.ppi_data.shape[0])

        # Alessandro
        with Timer('Loading miR data ...'):
            with open(mir_file, "r") as f:
                miR = f.read().splitlines()
            TFNames = self.unique_tfs
            sort_idx = np.argsort(TFNames)
            self.s1 = sort_idx[np.searchsorted(TFNames, miR, sorter=sort_idx)]

        # Auxiliary dicts
        gene2idx = {x: i for i,x in enumerate(self.gene_names)}
        tf2idx = {x: i for i,x in enumerate(self.unique_tfs)}

        # =====================================================================
        # Network construction
        # =====================================================================
        with Timer('Calculating coexpression network ...'):
            self.correlation_matrix = np.corrcoef(self.expression_data)
            if np.isnan(self.correlation_matrix).any():
                np.fill_diagonal(self.correlation_matrix, 1)
                self.correlation_matrix = np.nan_to_num(self.correlation_matrix)

        with Timer('Creating motif network ...'):
            self.motif_matrix_unnormalized = np.zeros((self.num_tfs, self.num_genes))
            idx_tfs = [tf2idx[x] for x in self.motif_data[0]]
            idx_genes = [gene2idx[x] for x in self.motif_data[1]]
            idx = np.ravel_multi_index((idx_tfs, idx_genes), self.motif_matrix_unnormalized.shape)
            self.motif_matrix_unnormalized.ravel()[idx] = self.motif_data[2]

        with Timer('Creating PPI network ...'):
            self.ppi_matrix = np.identity(self.num_tfs)
            idx_tf1 = [tf2idx[x] for x in self.ppi_data[0]]
            idx_tf2 = [tf2idx[x] for x in self.ppi_data[1]]
            idx = np.ravel_multi_index((idx_tf1, idx_tf2), self.ppi_matrix.shape)
            self.ppi_matrix.ravel()[idx] = self.ppi_data[2]
            idx = np.ravel_multi_index((idx_tf2, idx_tf1), self.ppi_matrix.shape)
            self.ppi_matrix.ravel()[idx] = self.ppi_data[2]

        # =====================================================================
        # Network normalization
        # =====================================================================
        with Timer('Normalizing networks ...'):
            self.correlation_matrix = self._normalize_network(self.correlation_matrix)
            self.motif_matrix = self._normalize_network(self.motif_matrix_unnormalized)
            self.ppi_matrix = self._normalize_network(self.ppi_matrix)
        
        # =====================================================================
        # Clean up useless variables to release memory
        # =====================================================================
        if save_memory:
            print("Clearing motif and ppi data, unique tfs, and gene names for speed")
            del self.motif_data, self.ppi_data, self.unique_tfs, self.gene_names, self.motif_matrix_unnormalized

        # =====================================================================
        # Saving middle data to tmp
        # =====================================================================
        if save_tmp:
            with Timer('Saving expression matrix and normalized networks ...'):
                np.save('/tmp/expression.npy', self.expression_data.values)
                np.save('/tmp/motif.normalized.npy', self.motif_matrix)
                np.save('/tmp/ppi.normalized.npy', self.ppi_matrix)

        # Clean up useless variables to release memory
        del self.expression_data

        # =====================================================================
        # Running PUMA algorithm
        # =====================================================================
        print('Running PUMA algorithm ...')
        self.puma_network = self.puma_loop(self.correlation_matrix, self.motif_matrix, self.ppi_matrix)


    def _normalize_network(self, x):
        norm_col = zscore(x, axis=0)
        if x.shape[0] == x.shape[1]:
            norm_row = norm_col.T
        else:
            norm_row = zscore(x, axis=1)
        #Alessandro: replace nan values
        normalized_matrix = (norm_col + norm_row) / math.sqrt(2)
        norm_total = (x-np.mean(x))/np.std(x)   #NB zscore(x) is not the same
        nan_col = np.isnan(norm_col)
        nan_row = np.isnan(norm_row)
        normalized_matrix[nan_col] = (norm_row[nan_col] + norm_total[nan_col])/math.sqrt(2)
        normalized_matrix[nan_row] = (norm_col[nan_row] + norm_total[nan_row])/math.sqrt(2)
        normalized_matrix[nan_col & nan_row] = 2*norm_col[nan_col & nan_row]/math.sqrt(2)
        return normalized_matrix

    def puma_loop(self, correlation_matrix, motif_matrix, ppi_matrix):
        """Puma algorithm.
        """
        def t_function(x, y=None):
            '''T function.'''
            if y is None:
                a_matrix = np.dot(x, x.T)
                s = np.square(x).sum(axis=1)
                a_matrix /= np.sqrt(s + s.reshape(-1, 1) - np.abs(a_matrix))
            else:
                a_matrix = np.dot(x, y)
                a_matrix /= np.sqrt(np.square(y).sum(axis=0) + np.square(x).sum(axis=1).reshape(-1, 1) - np.abs(a_matrix))
            return a_matrix

        def update_diagonal(diagonal_matrix, num, alpha, step):
            '''Update diagonal.'''
            np.fill_diagonal(diagonal_matrix, np.nan)
            diagonal_std = np.nanstd(diagonal_matrix, 1)
            diagonal_fill = diagonal_std * num * math.exp(2 * alpha * step)
            np.fill_diagonal(diagonal_matrix, diagonal_fill)

        puma_loop_time = time.time()
        num_tfs, num_genes = motif_matrix.shape
        
        # Alessandro
        TFCoopInit = ppi_matrix.copy()

        step = 0
        hamming = 1
        alpha = 0.1
        while hamming > 0.001:
            # Update motif_matrix
            W = 0.5 * (t_function(ppi_matrix, motif_matrix) + t_function(motif_matrix, correlation_matrix))  # W = (R + A) / 2
            hamming = np.abs(motif_matrix - W).mean()
            motif_matrix *= (1 - alpha)
            motif_matrix += (alpha * W)

            if hamming > 0.001:
                # Update ppi_matrix
                ppi = t_function(motif_matrix)  # t_func(X, X.T)
                update_diagonal(ppi, num_tfs, alpha, step)
                ppi_matrix *= (1 - alpha)
                ppi_matrix += (alpha * ppi)

                # Alessandro
                TFCoopDiag = ppi_matrix.diagonal()
                ppi_matrix[self.s1] = TFCoopInit[self.s1]
                ppi_matrix[:, self.s1] = TFCoopInit[:, self.s1]
                np.fill_diagonal(ppi_matrix, TFCoopDiag)

                # Update correlation_matrix
                motif = t_function(motif_matrix.T)  # t_func(X.T, X)
                update_diagonal(motif, num_genes, alpha, step)
                correlation_matrix *= (1 - alpha)
                correlation_matrix += (alpha * motif)

                del W, ppi, motif  # release memory for next step

            print('step: {}, hamming: {}'.format(step, hamming))
            step = step + 1

        print('Running puma took: %.2f seconds!' % (time.time() - puma_loop_time))
        #Ale: reintroducing the export_panda_results array if Panda called with save_memory=False
        if hasattr(self,'unique_tfs'):
            tfs = np.tile(self.unique_tfs, (len(self.gene_names), 1)).flatten()
            genes = np.repeat(self.gene_names,self.num_tfs)
            motif = self.motif_matrix_unnormalized.flatten(order='F')
            force = self.motif_matrix.flatten(order='F')
            #self.export_puma_results = pd.DataFrame({'tf':tfs, 'gene': genes,'motif': motif, 'force': force})
            self.export_puma_results = np.column_stack((tfs,genes,motif,force))
        return motif_matrix

    def save_puma_results(self, path='puma.npy'):
        with Timer('Saving PUMA network to %s ...' % path):
            #Because there are two modes of operation (save_memory), save to file will be different
            if hasattr(self,'export_puma_results'):
                toexport = self.export_puma_results
            else:
                toexport = self.puma_network
            #Export to file
            if path.endswith('.txt'):
                np.savetxt(path, toexport,fmt='%s', delimiter=' ')
            elif path.endswith('.csv'):
                np.savetxt(path, toexport,fmt='%s', delimiter=',')
            elif path.endswith('.tsv'):
                np.savetxt(path, toexport,fmt='%s', delimiter='/t')
            else:
                np.save(path, toexport)
    def top_network_plot(self, top = 100, file = 'puma_top_100.png'):
        '''Select top genes.'''
        if not hasattr(self,'export_panda_results'):
            raise AttributeError("Panda object does not contain the export_panda_results attribute.\n"+
                "Run Panda with the flag save_memory=False")
        #Ale TODO: work in numpy instead of pandas?
        self.panda_results = pd.DataFrame(self.export_panda_results, columns=['tf','gene','motif','force'])
        subset_panda_results = self.panda_results.sort_values(by=['force'], ascending=False)
        subset_panda_results = subset_panda_results[subset_panda_results.tf != subset_panda_results.gene]
        subset_panda_results = subset_panda_results[0:top]
        self.__shape_plot_network(subset_panda_results = subset_panda_results, file = file)
        return None
    def __shape_plot_network(self, subset_panda_results, file = 'panda.png'):
        '''Create plot.'''
        #reshape data for networkx
        unique_genes = list(set(list(subset_panda_results['tf'])+list(subset_panda_results['gene'])))
        unique_genes = pd.DataFrame(unique_genes)
        unique_genes.columns = ['name']
        unique_genes['index'] = unique_genes.index
        subset_panda_results = subset_panda_results.merge(unique_genes, how='inner', left_on='tf', right_on='name')
        subset_panda_results = subset_panda_results.rename(columns = {'index': 'tf_index'})
        subset_panda_results = subset_panda_results.drop(['name'], 1)
        subset_panda_results = subset_panda_results.merge(unique_genes, how='inner', left_on='gene', right_on='name')
        subset_panda_results = subset_panda_results.rename(columns = {'index': 'gene_index'})
        subset_panda_results = subset_panda_results.drop(['name'], 1)
        links = subset_panda_results[['tf_index', 'gene_index', 'force']]
        self.__create_plot(unique_genes = unique_genes, links = links, file = file)
        return None
    def __create_plot(self, unique_genes, links, file = 'panda.png'):
        '''Run plot.'''
        import networkx as nx
        import matplotlib.pyplot as plt
        g = nx.Graph()
        g.clear()
        plt.clf()
        g.add_nodes_from(unique_genes['index'])
        edges = []
        for i in range(0, len(links)):
            edges = edges + [(links.iloc[i]['tf_index'], links.iloc[i]['gene_index'], float(links.iloc[i]['force'])/200)]
        g.add_weighted_edges_from(edges)
        labels = {}
        for i in range(0, len(unique_genes)):
            labels[unique_genes.iloc[i]['index']] = unique_genes.iloc[i]['name']
        pos = nx.spring_layout(g)
        nx.draw_networkx(g, pos, labels=labels,
                 node_size=20, font_size=3,
                 alpha=0.3, linewidth = 0.5, width =0.5)
        plt.axis('off')
        plt.savefig(file, dpi=300)
        return None
